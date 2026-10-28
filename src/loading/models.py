"""Validated destination configuration."""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.security.secrets import SecretRef

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,120}$")
SQLITE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,100}\.(db|sqlite|sqlite3)$")
HOST = re.compile(r"^[A-Za-z0-9.\-_:\[\]]{1,253}$")


class DestinationType(str, Enum):
    CSV = "csv"
    PARQUET = "parquet"
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class LoadStrategy(str, Enum):
    REPLACE = "replace"
    APPEND = "append"
    UPSERT = "upsert"


DESTINATION_LABELS = {
    DestinationType.CSV: "CSV file",
    DestinationType.PARQUET: "Parquet file",
    DestinationType.SQLITE: "SQLite database",
    DestinationType.POSTGRESQL: "PostgreSQL database",
}
STRATEGY_LABELS = {
    LoadStrategy.REPLACE: "Replace - delete existing data and load the new data",
    LoadStrategy.APPEND: "Append - add the new records",
    LoadStrategy.UPSERT: "Upsert - update matching records, insert new ones",
}
FILE_TYPES = {DestinationType.CSV, DestinationType.PARQUET}
SQL_TYPES = {DestinationType.SQLITE, DestinationType.POSTGRESQL}


def validate_identifier(value: str, what: str = "Table name") -> str:
    value = value.strip()
    if not IDENTIFIER.match(value):
        raise ValueError(
            f"{what} must start with a letter or underscore and contain only letters, digits and "
            "underscores (max 63 characters)."
        )
    return value


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="postgres", min_length=1, max_length=63)
    username: str = Field(default="postgres", min_length=1, max_length=63)
    password: SecretRef | None = None
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    db_schema: str = "public"
    url_env: str | None = None   # name of an env var holding a full connection URL
    connect_timeout: int = Field(default=10, ge=1, le=120)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip()
        if not HOST.match(value):
            raise ValueError("Host must be a host name or IP address.")
        return value

    @field_validator("db_schema")
    @classmethod
    def _schema(cls, value: str) -> str:
        return validate_identifier(value, "Schema name")

    @field_validator("url_env")
    @classmethod
    def _env(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$", value.strip()):
            raise ValueError("Environment variable names may contain letters, digits and underscores only.")
        return value.strip()


class DestinationConfig(BaseModel):
    """Where and how the transformed data is loaded."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Destination", min_length=1, max_length=120)
    type: DestinationType
    file_name: str | None = None
    sqlite_file: str | None = "warehouse.db"
    postgres: PostgresConfig | None = None
    table: str | None = None
    strategy: LoadStrategy = LoadStrategy.APPEND
    upsert_keys: list[str] = Field(default_factory=list)
    confirm_replace: bool = False

    @field_validator("table")
    @classmethod
    def _table(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_identifier(value)

    @field_validator("sqlite_file")
    @classmethod
    def _sqlite_file(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not SQLITE_FILE.match(value):
            raise ValueError("SQLite file name must look like warehouse.db (letters, digits, _ . - only).")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "DestinationConfig":
        if self.type in FILE_TYPES:
            if not self.file_name:
                raise ValueError("Enter an output file name.")
            name = self.file_name.strip()
            extension = "." + self.type.value
            if not name.lower().endswith(extension):
                name += extension
            stem = name[: -len(extension)]
            if not FILE_NAME.match(stem) or ".." in name:
                raise ValueError("The file name may contain letters, digits, spaces, _ . - only (no folders).")
            self.file_name = name
            if self.strategy == LoadStrategy.UPSERT:
                raise ValueError("Upsert is supported for SQLite and PostgreSQL destinations only.")
        else:
            if not self.table:
                raise ValueError("Enter a destination table name.")
            if self.type == DestinationType.SQLITE and not self.sqlite_file:
                raise ValueError("Enter a SQLite database file name.")
            if self.type == DestinationType.POSTGRESQL and self.postgres is None:
                raise ValueError("PostgreSQL connection settings are required.")
        if self.strategy == LoadStrategy.UPSERT:
            keys = [k.strip() for k in self.upsert_keys if k and k.strip()]
            if not keys:
                raise ValueError("Choose at least one unique key column for upsert.")
            self.upsert_keys = keys
        return self

    @property
    def is_file(self) -> bool:
        return self.type in FILE_TYPES

    def target_label(self) -> str:
        if self.type in FILE_TYPES:
            return f"data/output/{self.file_name}"
        if self.type == DestinationType.SQLITE:
            return f"{self.sqlite_file} → {self.table}"
        pg = self.postgres
        where = f"env:{pg.url_env}" if pg and pg.url_env else (f"{pg.host}:{pg.port}/{pg.database}" if pg else "?")
        schema = pg.db_schema if pg else "public"
        return f"{where} → {schema}.{self.table}"
