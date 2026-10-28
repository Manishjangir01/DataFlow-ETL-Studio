"""Phase 4 tests: database and file loading (replace, append, upsert).

SQLite tests always run. The same tests run against a real PostgreSQL server when
``DATAFLOW_TEST_POSTGRES_URL`` is set, e.g.::

    DATAFLOW_TEST_POSTGRES_URL=postgresql+psycopg://etl:secret@localhost:5432/dataflow_test
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text

from src.loading import sql_loader
from src.loading.loader import destructive_warning, inspect_target, load_data, preview_target, check_destination
from src.loading.models import DestinationConfig
from src.loading.sql_loader import TargetInfo, connect
from src.security.secrets import SecretBox, generate_key
from src.utils.exceptions import LoadError

PG_URL = os.environ.get("DATAFLOW_TEST_POSTGRES_URL")
BACKENDS = ["sqlite"] + (["postgresql"] if PG_URL else [])


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(generate_key())


@pytest.fixture(params=BACKENDS)
def make_dest(request):
    backend = request.param
    table = f"t_{uuid.uuid4().hex[:8]}"

    def factory(**kwargs) -> DestinationConfig:
        base = {"type": backend, "table": table, "strategy": "append"}
        if backend == "sqlite":
            base["sqlite_file"] = "test_warehouse.db"
        else:
            base["postgres"] = {"url_env": "DATAFLOW_TEST_POSTGRES_URL", "db_schema": "public"}
        base.update(kwargs)
        return DestinationConfig.model_validate(base)

    factory.backend = backend
    yield factory
    if backend == "postgresql":
        dest = factory()
        from src.utils.config import load_settings

        engine = connect(dest, load_settings({}, load_env_file=False), SecretBox(generate_key()))
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        engine.dispose()


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "name": ["Ann", "Bob", None],
            "amount": [10.5, None, 3.0],
            "active": [True, False, True],
            "joined": [dt.date(2024, 1, 1), dt.date(2024, 2, 1), None],
            "updated_at": pd.to_datetime(["2024-01-01 10:00", "2024-01-02 11:30", None]),
            "count": pd.array([5, None, 7], dtype="Int64"),
        }
    )


def rows(dest, settings, box) -> pd.DataFrame:
    return preview_target(dest, settings, box, limit=1000).sort_values("id").reset_index(drop=True)


# --- SQL ----------------------------------------------------------------------------

def test_append_creates_table_and_preserves_types(make_dest, settings, box):
    dest = make_dest()
    result = load_data(frame(), dest, settings, box)
    assert result.rows_loaded == 3 and result.verified
    info = inspect_target(dest, settings, box)
    assert info.exists and info.row_count == 3
    data = rows(dest, settings, box)
    assert data["name"].tolist()[:2] == ["Ann", "Bob"] and pd.isna(data.loc[2, "name"])
    assert bool(data.loc[0, "active"]) is True
    assert pd.Timestamp(data.loc[0, "joined"]).date() == dt.date(2024, 1, 1)
    assert int(data.loc[2, "count"]) == 7
    again = load_data(frame(), dest, settings, box)
    assert again.verification.startswith("Table grew from 3 to 6")
    assert inspect_target(dest, settings, box).row_count == 6


def test_append_rejects_unknown_columns_without_changes(make_dest, settings, box):
    dest = make_dest()
    load_data(frame(), dest, settings, box)
    extra = frame().assign(surprise=1)
    with pytest.raises(LoadError) as info:
        load_data(extra, dest, settings, box)
    assert "surprise" in info.value.message
    assert inspect_target(dest, settings, box).row_count == 3


def test_replace_requires_confirmation_and_swaps_atomically(make_dest, settings, box, monkeypatch):
    dest = make_dest()
    load_data(frame(), dest, settings, box)
    with pytest.raises(LoadError):
        load_data(frame().head(1), make_dest(strategy="replace"), settings, box)
    replace = make_dest(strategy="replace", confirm_replace=True)
    assert destructive_warning(replace, inspect_target(replace, settings, box)).startswith("Replace will permanently delete 3")
    result = load_data(frame().head(2).drop(columns=["count"]), replace, settings, box)
    assert result.verification == "Table contains exactly 2 rows"
    info = inspect_target(dest, settings, box)
    assert info.row_count == 2 and "count" not in info.columns

    # A failure during the load must leave the previous table untouched.
    original_insert = sql_loader._insert

    def failing_insert(conn, table, df, dialect):
        original_insert(conn, table, df.head(1), dialect)
        raise RuntimeError("disk full")

    monkeypatch.setattr(sql_loader, "_insert", failing_insert)
    with pytest.raises(RuntimeError):
        load_data(frame(), replace, settings, box)
    monkeypatch.setattr(sql_loader, "_insert", original_insert)
    assert inspect_target(dest, settings, box).row_count == 2
    engine = connect(dest, settings, box)
    with engine.connect() as conn:
        leftovers = [t for t in inspect(conn).get_table_names() if t.startswith("_dfstage_")]
    engine.dispose()
    assert leftovers == []


def test_upsert_inserts_updates_and_reports(make_dest, settings, box):
    dest = make_dest(strategy="upsert", upsert_keys=["id"])
    first = load_data(frame(), dest, settings, box)
    assert (first.rows_inserted, first.rows_updated) == (3, 0)
    batch = pd.DataFrame(
        {
            "id": [2, 4, None, 4],
            "name": ["Bobby", "Dan", "nokey", "Dana"],
            "amount": [99.0, 1.0, 1.0, 2.0],
            "active": [True, True, True, False],
            "joined": [None, None, None, None],
            "updated_at": pd.to_datetime([None, None, None, None]),
            "count": pd.array([1, 2, 3, 4], dtype="Int64"),
        }
    )
    second = load_data(batch, dest, settings, box)
    assert (second.rows_inserted, second.rows_updated) == (1, 1)
    assert second.rows_skipped == 1 and second.duplicates_in_batch == 1
    data = rows(dest, settings, box)
    assert data["id"].tolist() == [1, 2, 3, 4]
    assert data.loc[1, "name"] == "Bobby" and data.loc[3, "name"] == "Dana"
    assert data.loc[0, "name"] == "Ann"


def test_upsert_composite_key_and_existing_duplicates(make_dest, settings, box):
    dest = make_dest(strategy="upsert", upsert_keys=["region", "sku"])
    base = pd.DataFrame({"region": ["IN", "IN", "US"], "sku": ["A", "B", "A"], "qty": [1, 2, 3]})
    load_data(base, dest, settings, box)
    update = pd.DataFrame({"region": ["IN", "US"], "sku": ["B", "B"], "qty": [20, 30]})
    result = load_data(update, dest, settings, box)
    assert (result.rows_inserted, result.rows_updated) == (1, 1)
    assert inspect_target(dest, settings, box).row_count == 4

    dupes = make_dest(table=f"d_{uuid.uuid4().hex[:8]}")
    load_data(pd.DataFrame({"k": [1, 1], "v": [1, 2]}), dupes, settings, box)
    with pytest.raises(LoadError) as info:
        load_data(pd.DataFrame({"k": [1], "v": [3]}), dupes.model_copy(update={"strategy": "upsert", "upsert_keys": ["k"]}), settings, box)
    assert "duplicate keys" in info.value.message
    if make_dest.backend == "postgresql":
        engine = connect(dupes, settings, box)
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{dupes.table}"'))
        engine.dispose()


def test_connection_check_and_preview(make_dest, settings, box):
    dest = make_dest()
    assert "Connected" in check_destination(dest, settings, box)
    assert preview_target(dest, settings, box).empty


@pytest.mark.parametrize("bad", ["users; DROP TABLE x", "1abc", "a-b", "x" * 64, "", "名前"])
def test_table_name_validation(bad):
    with pytest.raises(ValidationError):
        DestinationConfig(type="sqlite", table=bad or None, sqlite_file="w.db")


def test_destination_config_rules():
    with pytest.raises(ValidationError):
        DestinationConfig(type="csv", file_name="../../etc/passwd")
    with pytest.raises(ValidationError):
        DestinationConfig(type="csv", file_name="out.csv", strategy="upsert", upsert_keys=["id"])
    with pytest.raises(ValidationError):
        DestinationConfig(type="sqlite", table="t", sqlite_file="w.db", strategy="upsert")
    with pytest.raises(ValidationError):
        DestinationConfig(type="sqlite", table="t", sqlite_file="../w.db")
    assert DestinationConfig(type="parquet", file_name="sales").file_name == "sales.parquet"


def test_long_column_names_are_rejected(settings, box):
    dest = DestinationConfig(type="sqlite", table="wide", sqlite_file="w.db")
    with pytest.raises(LoadError) as info:
        load_data(pd.DataFrame({"x" * 70: [1]}), dest, settings, box)
    assert "Standardize column names" in (info.value.hint or "")


def test_postgres_password_is_encrypted_and_errors_are_redacted(settings, box):
    dest = DestinationConfig(
        type="postgresql", table="t",
        postgres={"host": "127.0.0.1", "port": 1, "database": "nope", "username": "u",
                  "password": box.encrypt("Sup3r-Secret-PW").model_dump(), "connect_timeout": 2},
    )
    assert "Sup3r-Secret-PW" not in dest.model_dump_json()
    with pytest.raises(LoadError) as info:
        check_destination(dest, settings, box)
    assert "Sup3r-Secret-PW" not in str(info.value)
    url = sql_loader.build_url(dest, settings, box)
    assert "Sup3r-Secret-PW" not in str(url)   # SQLAlchemy masks the password when printed


# --- Files ----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["csv", "parquet"])
def test_file_replace_append_and_verify(kind, settings, box):
    dest = DestinationConfig(type=kind, file_name=f"export_{kind}", strategy="append")
    first = load_data(frame(), dest, settings, box)
    assert first.verified and first.rows_loaded == 3
    second = load_data(frame(), dest, settings, box)
    assert inspect_target(dest, settings, box).row_count == 6 and second.verification.startswith("File now has 6")
    with pytest.raises(LoadError):
        load_data(frame().drop(columns=["name"]), dest, settings, box)
    assert inspect_target(dest, settings, box).row_count == 6
    with pytest.raises(LoadError):
        load_data(frame(), dest.model_copy(update={"strategy": "replace"}), settings, box)
    replace = dest.model_copy(update={"strategy": "replace", "confirm_replace": True})
    load_data(frame().head(1), replace, settings, box)
    assert inspect_target(dest, settings, box).row_count == 1
    assert not list(settings.output_dir.glob(".*.tmp"))
    assert len(preview_target(dest, settings, box)) == 1


def test_csv_handles_newlines_and_quotes(settings, box):
    dest = DestinationConfig(type="csv", file_name="tricky.csv")
    df = pd.DataFrame({"text": ['line1\nline2', 'quote " inside', "comma, here"]})
    assert load_data(df, dest, settings, box).rows_loaded == 3
    assert inspect_target(dest, settings, box).row_count == 3


def test_destructive_warning_only_for_existing_targets():
    dest = DestinationConfig(type="csv", file_name="a.csv", strategy="replace", confirm_replace=True)
    assert destructive_warning(dest, TargetInfo(False, None, [], "x")) is None
    assert destructive_warning(dest.model_copy(update={"strategy": "append"}), None) is None
