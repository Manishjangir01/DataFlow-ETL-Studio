"""Turn JSON payloads into tabular records.

* :func:`find_record_paths` lists every field in a JSON document that holds a list of
  objects, so users can pick "the field containing the records".
* :func:`extract_records` returns the records found at a path (or auto-detects them).
* :func:`records_to_dataframe` flattens nested objects into ``parent.child`` columns and
  stores remaining lists / objects as JSON text so every cell is loadable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.utils.exceptions import ExtractionError

# Field names commonly used by APIs for the record list, in priority order.
_PREFERRED_RECORD_KEYS = (
    "data", "results", "items", "records", "rows", "entries", "value", "objects", "hits", "list",
)
_MAX_SEARCH_DEPTH = 4


@dataclass(frozen=True)
class RecordPathCandidate:
    """A JSON field that contains a list of records."""

    path: str          # dot path, "" means the document root
    record_count: int
    sample_keys: tuple[str, ...]

    @property
    def label(self) -> str:
        where = self.path or "(root)"
        keys = ", ".join(self.sample_keys[:6])
        return f"{where} - {self.record_count} records [{keys}]"


def get_path(document: Any, path: str | None) -> Any:
    """Return the value at a dot path such as ``data.items`` or ``response.0.rows``.

    Raises:
        ExtractionError: when the path does not exist in the document.
    """
    if path is None or path.strip() in ("", "$", "."):
        return document
    current = document
    walked: list[str] = []
    for part in path.strip().lstrip("$.").split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            if part not in current:
                available = ", ".join(list(current.keys())[:20]) or "(none)"
                where = ".".join(walked) or "root"
                raise ExtractionError(
                    f"Field '{part}' was not found at {where}. Available fields: {available}.",
                    hint="Use 'Test connection' to see the fields in the response.",
                )
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise ExtractionError(f"List index {index} is out of range at '{'.'.join(walked)}'.")
            current = current[index]
        else:
            raise ExtractionError(
                f"Cannot read field '{part}' because '{'.'.join(walked) or 'root'}' is "
                f"a {type(current).__name__}, not an object."
            )
        walked.append(part)
    return current


def _is_record_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(isinstance(v, dict) for v in value[:50])


def find_record_paths(document: Any, max_depth: int = _MAX_SEARCH_DEPTH) -> list[RecordPathCandidate]:
    """Return every field (up to ``max_depth`` levels deep) holding a list of objects."""
    found: list[RecordPathCandidate] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if _is_record_list(node):
            keys: list[str] = []
            for record in node[:20]:
                for key in record.keys():
                    if key not in keys:
                        keys.append(str(key))
            found.append(RecordPathCandidate(path, len(node), tuple(keys)))
            return
        if depth >= max_depth:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                walk(value, child, depth + 1)

    walk(document, "", 0)

    def priority(candidate: RecordPathCandidate) -> tuple[int, int, int]:
        leaf = candidate.path.split(".")[-1].lower() if candidate.path else ""
        preferred = _PREFERRED_RECORD_KEYS.index(leaf) if leaf in _PREFERRED_RECORD_KEYS else 99
        return (candidate.path.count("."), preferred, -candidate.record_count)

    return sorted(found, key=priority)


def extract_records(document: Any, records_path: str | None = None) -> list[dict[str, Any]]:
    """Return the list of record dictionaries in ``document``.

    Without a path: a top-level list is used directly; for an object the best
    candidate from :func:`find_record_paths` is used; otherwise the object itself is
    treated as a single record.
    """
    if records_path:
        target = get_path(document, records_path)
    elif isinstance(document, list):
        target = document
    elif isinstance(document, dict):
        candidates = find_record_paths(document)
        target = get_path(document, candidates[0].path) if candidates else [document]
    else:
        raise ExtractionError(
            f"Expected a JSON object or array but received {type(document).__name__}."
        )

    if isinstance(target, dict):
        return [target]
    if not isinstance(target, list):
        raise ExtractionError(
            f"The field '{records_path}' contains a {type(target).__name__}, not a list of records.",
            hint="Choose a field that contains a JSON array.",
        )
    records: list[dict[str, Any]] = []
    for item in target:
        if isinstance(item, dict):
            records.append(item)
        elif item is None:
            continue
        else:
            # Lists of scalars become a single 'value' column.
            records.append({"value": item})
    return records


def _to_json_text(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    if isinstance(value, np.ndarray):
        return json.dumps(value.tolist(), ensure_ascii=False, default=str)
    return value


def make_cells_scalar(df: pd.DataFrame) -> pd.DataFrame:
    """Convert list / dict cells into JSON strings so the frame is hashable and loadable."""
    result = df
    for column in df.columns:
        series = df[column]
        if series.dtype != object:
            continue
        sample = series.dropna()
        if sample.empty:
            continue
        if sample.map(lambda v: isinstance(v, (dict, list, tuple, np.ndarray))).any():
            if result is df:
                result = df.copy()
            result[column] = series.map(_to_json_text)
    return result


def records_to_dataframe(
    records: Iterable[dict[str, Any]],
    *,
    flatten: bool = True,
    separator: str = ".",
    max_level: int | None = None,
) -> pd.DataFrame:
    """Convert records to a DataFrame, flattening nested objects into columns."""
    records = list(records)
    if not records:
        return pd.DataFrame()
    if flatten:
        df = pd.json_normalize(records, sep=separator, max_level=max_level)
    else:
        df = pd.DataFrame.from_records(records)
    df.columns = [str(c) for c in df.columns]
    return make_cells_scalar(df)
