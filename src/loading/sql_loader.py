"""Load DataFrames into SQLite or PostgreSQL with replace / append / upsert.

Safety guarantees:

* Table and schema names are validated identifiers and always quoted by SQLAlchemy.
* All values are sent as bound parameters (no string-built SQL with data).
* Each load runs in **one transaction**. Replace loads into a staging table and only
  swaps it in after every row was written, so a failure leaves the old table intact.
* The row count is verified inside the transaction before commit; a mismatch rolls
  everything back and the run is reported as failed.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator

import numpy as np
import pandas as pd
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Engine, Float, Index, Integer, MetaData, String, Table, Text,
    func, inspect, select, text, tuple_,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, URL
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.loading.models import DestinationConfig, DestinationType, LoadStrategy
from src.security.redaction import redact_text
from src.security.secrets import SecretBox
from src.storage.engine import make_engine
from src.utils.config import Settings
from src.utils.exceptions import ConfigurationError, LoadError

CHUNK_SIZE = 1000
KEY_CHUNK = 400
MAX_IDENTIFIER = 63


@dataclass
class LoadResult:
    destination_type: str
    target: str
    strategy: str
    rows_loaded: int
    rows_inserted: int | None = None
    rows_updated: int | None = None
    rows_skipped: int = 0
    duplicates_in_batch: int = 0
    verified: bool = False
    verification: str = ""
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TargetInfo:
    exists: bool
    row_count: int | None
    columns: list[str]
    location: str
    message: str = ""


# ---------------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------------

def sqlite_path(dest: DestinationConfig, settings: Settings):
    base = settings.databases_dir.resolve()
    path = (base / (dest.sqlite_file or "warehouse.db")).resolve()
    if path.parent != base:
        raise ConfigurationError("The SQLite file must be inside data/databases.")
    return path


def build_url(dest: DestinationConfig, settings: Settings, secret_box: SecretBox, env: dict[str, str] | None = None) -> URL | str:
    """Build the connection URL (passwords are resolved here and never logged)."""
    import os

    if dest.type == DestinationType.SQLITE:
        base = settings.databases_dir
        base.mkdir(parents=True, exist_ok=True)
        return "sqlite:///" + sqlite_path(dest, settings).as_posix()
    if dest.type == DestinationType.POSTGRESQL and dest.postgres is not None:
        pg = dest.postgres
        environment = os.environ if env is None else env
        if pg.url_env:
            raw = environment.get(pg.url_env)
            if not raw:
                raise ConfigurationError(
                    f"Environment variable {pg.url_env} is not set.",
                    hint="Add the PostgreSQL URL to your .env file and restart the app.",
                )
            from src.security.redaction import register_secret
            from sqlalchemy.engine import make_url

            url = make_url(raw)
            if url.password:
                register_secret(str(url.password))
            if url.drivername in ("postgresql", "postgres"):
                url = url.set(drivername="postgresql+psycopg")
            if not url.drivername.startswith("postgresql"):
                raise ConfigurationError(f"{pg.url_env} must contain a postgresql:// URL.")
            return url
        password = secret_box.resolve(pg.password) if pg.password else None
        return URL.create(
            "postgresql+psycopg",
            username=pg.username,
            password=password,
            host=pg.host,
            port=pg.port,
            database=pg.database,
            query={"sslmode": pg.sslmode, "application_name": "dataflow-etl-studio"},
        )
    raise ConfigurationError(f"{dest.type.value} is not a database destination.")


def connect(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> Engine:
    url = build_url(dest, settings, secret_box)
    timeout = dest.postgres.connect_timeout if dest.postgres else 10
    return make_engine(url, connect_timeout=timeout)


def _schema(dest: DestinationConfig) -> str | None:
    if dest.type == DestinationType.POSTGRESQL and dest.postgres:
        return dest.postgres.db_schema
    return None


def _db_error(exc: Exception, action: str) -> LoadError:
    original = getattr(exc, "orig", None) or exc
    message = str(original).strip().splitlines()[0] if str(original).strip() else type(original).__name__
    hint = None
    lowered = message.lower()
    if "password authentication failed" in lowered or "authentication" in lowered:
        hint = "Check the database user name and password."
    elif "could not translate host name" in lowered or "connection refused" in lowered or "timeout" in lowered:
        hint = "Check that the database server is running and reachable (host and port)."
    elif "does not exist" in lowered and "database" in lowered:
        hint = "Create the database first or fix its name."
    elif "permission denied" in lowered:
        hint = "The database user lacks permission for this table or schema."
    elif "depend" in lowered:
        hint = "Other objects (views) depend on this table; replace is not possible."
    return LoadError(f"{action}: {redact_text(message)}", hint=hint)


# ---------------------------------------------------------------------------------
# Types and values
# ---------------------------------------------------------------------------------

def _object_kind(values: pd.Series) -> str:
    sample = values.dropna()
    if sample.empty:
        return "text"
    kinds = set()
    for v in sample.head(1000):
        if isinstance(v, (bool, np.bool_)):
            kinds.add("bool")
        elif isinstance(v, (int, np.integer)):
            kinds.add("int")
        elif isinstance(v, (float, np.floating, decimal.Decimal)):
            kinds.add("float")
        elif isinstance(v, (dt.datetime, pd.Timestamp)):
            kinds.add("datetime")
        elif isinstance(v, dt.date):
            kinds.add("date")
        else:
            kinds.add("text")
    if kinds == {"bool"}:
        return "bool"
    if kinds == {"int"}:
        return "int"
    if kinds <= {"int", "float"}:
        return "float"
    if kinds == {"date"}:
        return "date"
    if kinds <= {"datetime", "date"}:
        return "datetime"
    return "text"


def sql_type_for(series: pd.Series, dialect: str):
    if pd.api.types.is_bool_dtype(series):
        return Boolean()
    if pd.api.types.is_integer_dtype(series):
        return BigInteger()
    if pd.api.types.is_float_dtype(series):
        return Float(precision=53)
    if pd.api.types.is_datetime64_any_dtype(series):
        has_tz = getattr(series.dt, "tz", None) is not None
        return DateTime(timezone=has_tz and dialect == "postgresql")
    kind = _object_kind(series)
    return {
        "bool": Boolean(), "int": BigInteger(), "float": Float(precision=53),
        "date": Date(), "datetime": DateTime(), "text": Text(),
    }[kind]


def _check_columns(df: pd.DataFrame) -> None:
    names = [str(c) for c in df.columns]
    if not names:
        raise LoadError("There are no columns to load.")
    too_long = [n for n in names if len(n) > MAX_IDENTIFIER]
    if too_long:
        raise LoadError(
            f"Column names longer than {MAX_IDENTIFIER} characters: {', '.join(n[:30] + '…' for n in too_long)}.",
            hint="Add the 'Standardize column names' or 'Rename columns' transformation.",
        )
    bad = [n for n in names if not n.strip() or "\x00" in n]
    if bad:
        raise LoadError("Column names must not be empty.")
    lowered = [n.lower() for n in names]
    dupes = sorted({n for n in lowered if lowered.count(n) > 1})
    if dupes:
        raise LoadError(f"Column names differ only by case: {', '.join(dupes)}.", hint="Rename them first.")


def _convert(value: Any, column_type: Any, dialect: str) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(column_type, (String, Text)):
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        return value if isinstance(value, str) else str(value)
    if isinstance(column_type, Integer):
        if isinstance(value, float) and value.is_integer():
            return int(value)
    if isinstance(column_type, DateTime):
        if isinstance(value, dt.datetime) and value.tzinfo is not None and (dialect == "sqlite" or not column_type.timezone):
            return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return dt.datetime(value.year, value.month, value.day)
    if isinstance(column_type, Date) and not isinstance(column_type, DateTime) and isinstance(value, dt.datetime):
        return value.date()
    return value


def _rows(df: pd.DataFrame, table: Table, dialect: str) -> Iterator[list[dict[str, Any]]]:
    columns = [str(c) for c in df.columns]
    types = [table.c[c].type for c in columns]
    batch: list[dict[str, Any]] = []
    for row in df.itertuples(index=False, name=None):
        batch.append({c: _convert(v, t, dialect) for c, v, t in zip(columns, row, types)})
        if len(batch) >= CHUNK_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------

def _new_table(name: str, df: pd.DataFrame, schema: str | None, dialect: str, metadata: MetaData | None = None) -> Table:
    metadata = metadata or MetaData(schema=schema)
    columns = [Column(str(c), sql_type_for(df[c], dialect), nullable=True) for c in df.columns]
    return Table(name, metadata, *columns, schema=schema)


def _reflect(conn: Connection, name: str, schema: str | None) -> Table | None:
    if not inspect(conn).has_table(name, schema=schema):
        return None
    return Table(name, MetaData(), schema=schema, autoload_with=conn)


def _count(conn: Connection, table: Table) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _insert(conn: Connection, table: Table, df: pd.DataFrame, dialect: str) -> int:
    written = 0
    for batch in _rows(df, table, dialect):
        conn.execute(table.insert(), batch)
        written += len(batch)
    return written


def _quoted(conn: Connection, name: str, schema: str | None) -> str:
    preparer = conn.dialect.identifier_preparer
    return f"{preparer.quote_schema(schema)}.{preparer.quote(name)}" if schema else preparer.quote(name)


def _replace(conn: Connection, df: pd.DataFrame, dest: DestinationConfig, dialect: str) -> LoadResult:
    schema = _schema(dest)
    stage_name = f"_dfstage_{uuid.uuid4().hex[:12]}"
    stage = _new_table(stage_name, df, schema, dialect)
    stage.create(conn)
    written = _insert(conn, stage, df, dialect)
    counted = _count(conn, stage)
    if counted != len(df) or written != len(df):
        raise LoadError(f"Verification failed: expected {len(df)} rows in staging table, found {counted}.")
    existing = _reflect(conn, dest.table, schema)
    if existing is not None:
        conn.execute(text(f"DROP TABLE {_quoted(conn, dest.table, schema)}"))
    preparer = conn.dialect.identifier_preparer
    conn.execute(text(f"ALTER TABLE {_quoted(conn, stage_name, schema)} RENAME TO {preparer.quote(dest.table)}"))
    final = _reflect(conn, dest.table, schema)
    final_count = _count(conn, final)
    if final_count != len(df):
        raise LoadError(f"Verification failed: expected {len(df)} rows, found {final_count}.")
    return LoadResult(dest.type.value, dest.target_label(), "replace", len(df), rows_inserted=len(df), rows_updated=0,
                      verified=True, verification=f"Table contains exactly {final_count:,} rows")


def _compatible_table(conn: Connection, df: pd.DataFrame, dest: DestinationConfig, dialect: str) -> tuple[Table, bool]:
    schema = _schema(dest)
    table = _reflect(conn, dest.table, schema)
    if table is None:
        table = _new_table(dest.table, df, schema, dialect)
        table.create(conn)
        return table, True
    existing = {c.name for c in table.columns}
    extra = [str(c) for c in df.columns if str(c) not in existing]
    if extra:
        raise LoadError(
            f"The table '{dest.table}' has no column(s): {', '.join(extra)}.",
            hint="Remove/rename these columns in the transformations, or use the Replace strategy to recreate the table.",
        )
    return table, False


def _append(conn: Connection, df: pd.DataFrame, dest: DestinationConfig, dialect: str) -> LoadResult:
    table, created = _compatible_table(conn, df, dest, dialect)
    before = 0 if created else _count(conn, table)
    written = _insert(conn, table, df, dialect)
    after = _count(conn, table)
    if after - before != len(df) or written != len(df):
        raise LoadError(f"Verification failed: expected {len(df)} new rows, table grew by {after - before}.")
    note = " (table created)" if created else ""
    return LoadResult(dest.type.value, dest.target_label(), "append", len(df), rows_inserted=len(df), rows_updated=0,
                      verified=True, verification=f"Table grew from {before:,} to {after:,} rows{note}")


def _has_unique_on(conn: Connection, table: Table, keys: list[str]) -> bool:
    insp = inspect(conn)
    wanted = set(keys)
    pk = insp.get_pk_constraint(table.name, schema=table.schema) or {}
    if set(pk.get("constrained_columns") or []) == wanted:
        return True
    for uc in insp.get_unique_constraints(table.name, schema=table.schema):
        if set(uc.get("column_names") or []) == wanted:
            return True
    for ix in insp.get_indexes(table.name, schema=table.schema):
        if ix.get("unique") and set(ix.get("column_names") or []) == wanted:
            return True
    return False


def _key_value(value: Any, column_type: Any, dialect: str) -> Any:
    return _convert(value, column_type, dialect)


def _count_matching(conn: Connection, table: Table, keys: list[str], key_rows: list[tuple]) -> int:
    total = 0
    columns = [table.c[k] for k in keys]
    for start in range(0, len(key_rows), KEY_CHUNK):
        chunk = key_rows[start:start + KEY_CHUNK]
        if len(keys) == 1:
            condition = columns[0].in_([row[0] for row in chunk])
        else:
            condition = tuple_(*columns).in_(chunk)
        total += int(conn.execute(select(func.count()).select_from(table).where(condition)).scalar_one())
    return total


def _upsert(conn: Connection, df: pd.DataFrame, dest: DestinationConfig, dialect: str) -> LoadResult:
    keys = dest.upsert_keys
    missing = [k for k in keys if k not in df.columns]
    if missing:
        raise LoadError(f"Upsert key column(s) not in the data: {', '.join(missing)}.")
    warnings: list[str] = []
    key_present = df[keys].notna().all(axis=1)
    skipped = int((~key_present).sum())
    data = df.loc[key_present]
    if skipped:
        warnings.append(f"{skipped} row(s) with an empty key were not loaded.")
    duplicates = int(data.duplicated(subset=keys, keep="last").sum())
    if duplicates:
        warnings.append(f"{duplicates} row(s) repeated a key in this batch; the last one was kept.")
        data = data.drop_duplicates(subset=keys, keep="last")

    table, created = _compatible_table(conn, data, dest, dialect)
    absent = [k for k in keys if k not in table.c]
    if absent:
        raise LoadError(f"The table has no key column(s): {', '.join(absent)}.")
    if not _has_unique_on(conn, table, keys):
        digest = hashlib.sha1(f"{table.name}:{','.join(keys)}".encode()).hexdigest()[:10]
        index = Index(f"uq_dataflow_{digest}", *[table.c[k] for k in keys], unique=True)
        try:
            with conn.begin_nested():
                index.create(conn)
        except IntegrityError as exc:
            raise LoadError(
                f"Cannot add a unique index on ({', '.join(keys)}) because the existing table already has duplicate keys.",
                hint="Remove the duplicates in the table, or pick a different key.",
            ) from exc

    key_types = [table.c[k].type for k in keys]
    key_rows = [tuple(_key_value(v, t, dialect) for v, t in zip(row, key_types)) for row in data[keys].itertuples(index=False, name=None)]
    existing_before = 0 if created or not key_rows else _count_matching(conn, table, keys, key_rows)
    before_total = 0 if created else _count(conn, table)

    insert_fn = postgresql.insert if dialect == "postgresql" else sqlite.insert
    for batch in _rows(data, table, dialect):
        statement = insert_fn(table)
        updates = {c: statement.excluded[c] for c in data.columns if c not in keys}
        if updates:
            statement = statement.on_conflict_do_update(index_elements=keys, set_=updates)
        else:
            statement = statement.on_conflict_do_nothing(index_elements=keys)
        conn.execute(statement, batch)

    matched = _count_matching(conn, table, keys, key_rows) if key_rows else 0
    after_total = _count(conn, table)
    inserted = len(data) - existing_before
    if matched != len(data) or after_total - before_total != inserted:
        raise LoadError(
            f"Verification failed: expected {len(data)} keys present and {inserted} new rows, "
            f"found {matched} keys and {after_total - before_total} new rows."
        )
    return LoadResult(
        dest.type.value, dest.target_label(), "upsert", len(data), rows_inserted=inserted, rows_updated=existing_before,
        rows_skipped=skipped, duplicates_in_batch=duplicates, verified=True,
        verification=f"All {matched:,} keys present; {inserted:,} inserted, {existing_before:,} updated",
        warnings=warnings,
    )


def load_sql(df: pd.DataFrame, dest: DestinationConfig, settings: Settings, secret_box: SecretBox, *, engine: Engine | None = None) -> LoadResult:
    """Load ``df`` into the configured database table inside one transaction."""
    if dest.strategy == LoadStrategy.REPLACE and not dest.confirm_replace:
        raise LoadError(
            "The Replace strategy deletes the existing table and needs confirmation.",
            hint="Tick 'I understand existing data will be replaced' in the destination settings.",
        )
    _check_columns(df)
    started = time.perf_counter()
    owns_engine = engine is None
    engine = engine or connect(dest, settings, secret_box)
    dialect = engine.dialect.name
    try:
        with engine.begin() as conn:
            if dest.strategy == LoadStrategy.REPLACE:
                result = _replace(conn, df, dest, dialect)
            elif dest.strategy == LoadStrategy.APPEND:
                result = _append(conn, df, dest, dialect)
            else:
                result = _upsert(conn, df, dest, dialect)
    except LoadError:
        raise
    except SQLAlchemyError as exc:
        raise _db_error(exc, "Database load failed (all changes were rolled back)") from exc
    finally:
        if owns_engine:
            engine.dispose()
    result.duration_seconds = round(time.perf_counter() - started, 3)
    return result


def inspect_sql_target(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> TargetInfo:
    engine = connect(dest, settings, secret_box)
    try:
        with engine.connect() as conn:
            table = _reflect(conn, dest.table, _schema(dest))
            if table is None:
                return TargetInfo(False, None, [], dest.target_label(), "The table does not exist yet; it will be created.")
            return TargetInfo(True, _count(conn, table), [c.name for c in table.columns], dest.target_label())
    except SQLAlchemyError as exc:
        raise _db_error(exc, "Could not connect to the database") from exc
    finally:
        engine.dispose()


def preview_sql_target(dest: DestinationConfig, settings: Settings, secret_box: SecretBox, limit: int = 50) -> pd.DataFrame:
    engine = connect(dest, settings, secret_box)
    try:
        with engine.connect() as conn:
            table = _reflect(conn, dest.table, _schema(dest))
            if table is None:
                return pd.DataFrame()
            rows = conn.execute(select(table).limit(limit)).mappings().all()
            return pd.DataFrame(rows, columns=[c.name for c in table.columns])
    except SQLAlchemyError as exc:
        raise _db_error(exc, "Could not read the table") from exc
    finally:
        engine.dispose()


def check_sql_connection(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> str:
    engine = connect(dest, settings, secret_box)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            if engine.dialect.name == "postgresql":
                version = conn.execute(text("SHOW server_version")).scalar_one()
                return f"Connected to PostgreSQL {version}"
            return f"Connected to SQLite file {dest.sqlite_file}"
    except SQLAlchemyError as exc:
        raise _db_error(exc, "Connection failed") from exc
    finally:
        engine.dispose()


def list_sql_tables(dest: DestinationConfig, settings: Settings, secret_box: SecretBox) -> list[str]:
    engine = connect(dest, settings, secret_box)
    try:
        with engine.connect() as conn:
            return sorted(t for t in inspect(conn).get_table_names(schema=_schema(dest)) if not t.startswith("_dfstage_"))
    except SQLAlchemyError as exc:
        raise _db_error(exc, "Could not list tables") from exc
    finally:
        engine.dispose()


__all__: Iterable[str] = [
    "LoadResult", "TargetInfo", "build_url", "inspect_sql_target", "list_sql_tables", "load_sql",
    "preview_sql_target", "sql_type_for", "check_sql_connection",
]
