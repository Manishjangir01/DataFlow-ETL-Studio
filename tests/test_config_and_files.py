"""Phase 1 tests: settings validation, file readers and upload storage."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.extraction.file_reader import (
    FileReadOptions,
    detect_format,
    read_file_bytes,
    resolve_upload,
    safe_filename,
    save_upload,
)
from src.extraction.normalize import extract_records, find_record_paths, records_to_dataframe
from src.utils.config import load_settings
from src.utils.exceptions import ConfigurationError, ExtractionError, FileReadError, SecurityError


# --- Settings -------------------------------------------------------------------

def test_settings_defaults_and_relative_sqlite_path(settings_env):
    env = dict(settings_env, DATAFLOW_DATABASE_URL="sqlite:///data/test.db")
    settings = load_settings(env)
    assert settings.database_url.startswith("sqlite:///")
    assert settings.database_url.endswith("/data/test.db")
    assert settings.timezone == "UTC"
    assert settings.allow_http is False


def test_settings_reject_invalid_values(settings_env):
    with pytest.raises(ConfigurationError):
        load_settings(dict(settings_env, DATAFLOW_TIMEZONE="Mars/Base"))
    with pytest.raises(ConfigurationError):
        load_settings(dict(settings_env, DATAFLOW_MAX_UPLOAD_MB="lots"))
    with pytest.raises(ConfigurationError):
        load_settings(dict(settings_env, DATAFLOW_DATABASE_URL="mysql://x"))
    with pytest.raises(ConfigurationError):
        load_settings(dict(settings_env, DATAFLOW_ALLOW_HTTP="maybe"))


def test_settings_postgres_url_uses_psycopg_driver(settings_env):
    settings = load_settings(dict(settings_env, DATAFLOW_DATABASE_URL="postgresql://u:p@h/db"))
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_allowed_domains_parsed(settings_env):
    settings = load_settings(dict(settings_env, DATAFLOW_ALLOWED_DOMAINS=" API.Example.com, .foo.org ,"))
    assert settings.allowed_domains == ("api.example.com", "foo.org")


# --- File readers ------------------------------------------------------------------

def test_detect_format():
    assert detect_format("a.CSV") == "csv"
    assert detect_format("b.jsonl") == "json"
    assert detect_format("c.parquet") == "parquet"
    with pytest.raises(FileReadError):
        detect_format("malware.exe")


def test_read_sample_csv(samples_dir):
    result = read_file_bytes((samples_dir / "customers.csv").read_bytes(), "csv")
    df = result.dataframe
    assert len(df) == 21
    assert "email" in df.columns
    assert result.details["delimiter"] == ","


def test_read_csv_semicolon_and_text_mode():
    data = "id;code\n1;00123\n2;00456\n".encode()
    df = read_file_bytes(data, "csv", FileReadOptions(read_all_as_text=True)).dataframe
    assert list(df.columns) == ["id", "code"]
    assert df["code"].tolist() == ["00123", "00456"]


def test_read_csv_windows_encoding():
    data = "name,city\nJosé,Málaga\n".encode("cp1252")
    df = read_file_bytes(data, "csv").dataframe
    assert df.loc[0, "name"] == "José"


def test_read_csv_malformed_and_empty():
    with pytest.raises(FileReadError):
        read_file_bytes(b"", "csv")
    with pytest.raises(FileReadError):
        read_file_bytes(b"a,b\n1,2,3,4\n", "csv")


def test_read_nested_json_sample(samples_dir):
    df = read_file_bytes((samples_dir / "orders.json").read_bytes(), "json").dataframe
    assert len(df) == 12
    assert "customer.address.city" in df.columns
    # Lists are kept as JSON text so the frame stays loadable.
    assert isinstance(df.loc[0, "items"], str)
    assert json.loads(df.loc[0, "items"])[0]["sku"] == "SKU-1"


def test_read_json_with_records_path_and_jsonl(samples_dir):
    options = FileReadOptions(records_path="data.orders", flatten_nested=False)
    df = read_file_bytes((samples_dir / "orders.json").read_bytes(), "json", options).dataframe
    assert "customer" in df.columns and len(df) == 12
    jsonl = read_file_bytes((samples_dir / "events.jsonl").read_bytes(), "json").dataframe
    assert len(jsonl) == 8 and "user.plan" in jsonl.columns


def test_read_invalid_json():
    with pytest.raises(FileReadError):
        read_file_bytes(b"{not json", "json")


def test_read_parquet_sample(samples_dir):
    df = read_file_bytes((samples_dir / "products.parquet").read_bytes(), "parquet").dataframe
    assert len(df) == 10
    assert pd.api.types.is_datetime64_any_dtype(df["launched"])


def test_file_size_limit():
    with pytest.raises(FileReadError):
        read_file_bytes(b"a,b\n1,2\n", "csv", max_bytes=3)


def test_upload_storage_and_path_traversal(tmp_path):
    stored = save_upload(b"a,b\n1,2\n", "../../evil name.csv", tmp_path)
    assert "/" not in stored and stored.endswith("evil_name.csv")
    assert resolve_upload(stored, tmp_path).read_bytes().startswith(b"a,b")
    with pytest.raises(SecurityError):
        resolve_upload("../outside.csv", tmp_path)
    with pytest.raises(FileReadError):
        resolve_upload("missing.csv", tmp_path)
    assert safe_filename("C:\\Users\\me\\data file.csv") == "data_file.csv"


# --- JSON normalisation ---------------------------------------------------------

def test_find_record_paths_prefers_shallow_known_keys():
    doc = {"meta": {"tags": [{"a": 1}]}, "results": [{"id": 1}, {"id": 2}]}
    paths = find_record_paths(doc)
    assert paths[0].path == "results"
    assert {p.path for p in paths} == {"results", "meta.tags"}


def test_extract_records_variants():
    assert extract_records([{"a": 1}]) == [{"a": 1}]
    assert extract_records({"x": 1}) == [{"x": 1}]
    assert extract_records({"d": {"rows": [{"a": 1}]}}, "d.rows") == [{"a": 1}]
    assert extract_records([1, 2]) == [{"value": 1}, {"value": 2}]
    with pytest.raises(ExtractionError):
        extract_records({"d": 1}, "missing")
    with pytest.raises(ExtractionError):
        extract_records({"d": 5}, "d")


def test_records_to_dataframe_flattens():
    df = records_to_dataframe([{"a": {"b": 1, "c": [1, 2]}}])
    assert list(df.columns) == ["a.b", "a.c"]
    assert df.loc[0, "a.c"] == "[1, 2]"
