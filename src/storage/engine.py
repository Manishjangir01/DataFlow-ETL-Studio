"""SQLAlchemy engine factory with correct transaction handling for SQLite."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL


def make_engine(url: str | URL, *, connect_timeout: int = 10, wal: bool = False, **kwargs: Any) -> Engine:
    """Create an engine.

    For SQLite the pysqlite driver's implicit transaction handling is replaced by an
    explicit ``BEGIN`` so that DDL (CREATE / DROP / ALTER TABLE) is part of the same
    transaction as the data - a failed load then rolls back completely.
    """
    url_text = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
    if url_text.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False}, **kwargs)

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, _record):  # pragma: no cover - driver hook
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if wal:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):  # pragma: no cover - driver hook
            conn.exec_driver_sql("BEGIN")

        return engine

    connect_args = kwargs.pop("connect_args", {})
    connect_args.setdefault("connect_timeout", connect_timeout)
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args, **kwargs)
