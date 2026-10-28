"""Validated configuration models for data sources (REST APIs and files)."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.security.redaction import is_sensitive_key, redact_url
from src.security.secrets import SecretRef

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_PARAM_NAME = re.compile(r"^[^\r\n\x00]{1,128}$")
_BLOCKED_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


class AuthType(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"


class PaginationType(str, Enum):
    NONE = "none"
    PAGE_NUMBER = "page_number"
    OFFSET_LIMIT = "offset_limit"
    CURSOR = "cursor"
    NEXT_LINK = "next_link"


PAGINATION_LABELS = {
    PaginationType.NONE: "No pagination (single request)",
    PaginationType.PAGE_NUMBER: "Page number (?page=1&per_page=100)",
    PaginationType.OFFSET_LIMIT: "Offset / limit (?offset=0&limit=100)",
    PaginationType.CURSOR: "Cursor / token from the response",
    PaginationType.NEXT_LINK: "Next-page URL (response field or Link header)",
}
AUTH_LABELS = {
    AuthType.NONE: "None",
    AuthType.API_KEY: "API Key",
    AuthType.BEARER: "Bearer Token",
}


class AuthConfig(BaseModel):
    """Authentication settings. The secret itself is a :class:`SecretRef`."""

    model_config = ConfigDict(extra="forbid")

    type: AuthType = AuthType.NONE
    api_key_name: str = Field(default="X-API-Key", max_length=128)
    api_key_location: Literal["header", "query"] = "header"
    secret: SecretRef | None = None

    @field_validator("api_key_name")
    @classmethod
    def _valid_key_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not _PARAM_NAME.match(value):
            raise ValueError("API key name must be 1-128 characters without line breaks.")
        return value

    def require_secret(self) -> None:
        if self.type != AuthType.NONE and self.secret is None:
            raise ValueError(f"{AUTH_LABELS[self.type]} authentication needs a secret value.")


class PaginationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PaginationType = PaginationType.NONE
    # Page number
    page_param: str = "page"
    start_page: int = Field(default=1, ge=0, le=1_000_000)
    size_param: str | None = "per_page"
    page_size: int = Field(default=100, ge=1, le=10_000)
    # Offset / limit
    offset_param: str = "offset"
    limit_param: str = "limit"
    # Cursor
    cursor_param: str = "cursor"
    cursor_path: str = "next_cursor"
    # Next link (None = use the HTTP Link header rel="next")
    next_link_path: str | None = None
    # Safety limits
    max_pages: int = Field(default=50, ge=1, le=10_000)
    stop_on_empty_page: bool = True

    @field_validator("page_param", "offset_param", "limit_param", "cursor_param", "cursor_path")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value or not _PARAM_NAME.match(value):
            raise ValueError("Parameter names must be 1-128 characters without line breaks.")
        return value

    @field_validator("size_param", "next_link_path")
    @classmethod
    def _optional_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


class ApiSourceConfig(BaseModel):
    """Configuration of a REST API source."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2048)
    method: Literal["GET", "POST"] = "GET"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = None
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    timeout_seconds: float = Field(default=30.0, ge=1, le=300)
    max_records: int = Field(default=10_000, ge=1, le=1_000_000)
    records_path: str | None = None
    flatten_nested: bool = True
    max_retries: int = Field(default=3, ge=0, le=10)
    authorized: bool = False  # the user confirmed they may access this API

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("The URL must start with https:// (or http:// when allowed).")
        if any(ch in value for ch in "\r\n\t "):
            raise ValueError("The URL must not contain spaces or line breaks.")
        return value

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for name, value in headers.items():
            name = str(name).strip()
            if not name:
                continue
            if not _HEADER_NAME.match(name):
                raise ValueError(f"'{name}' is not a valid HTTP header name.")
            if name.lower() in _BLOCKED_HEADERS:
                raise ValueError(f"The '{name}' header is set automatically and cannot be overridden.")
            if is_sensitive_key(name):
                raise ValueError(
                    f"Header '{name}' looks like a credential. Use the Authentication "
                    "section so the value is encrypted and redacted."
                )
            value = str(value)
            if "\r" in value or "\n" in value:
                raise ValueError(f"Header '{name}' must not contain line breaks.")
            clean[name] = value
        return clean

    @field_validator("query_params")
    @classmethod
    def _check_params(cls, params: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for name, value in params.items():
            name = str(name).strip()
            if not name:
                continue
            if not _PARAM_NAME.match(name):
                raise ValueError(f"'{name}' is not a valid query parameter name.")
            if is_sensitive_key(name):
                raise ValueError(
                    f"Query parameter '{name}' looks like a credential. Use Authentication "
                    "> API Key with location 'query' instead."
                )
            clean[name] = str(value)
        return clean

    @field_validator("records_path")
    @classmethod
    def _clean_path(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @model_validator(mode="after")
    def _check_body(self) -> "ApiSourceConfig":
        if self.json_body is not None and self.method == "GET":
            raise ValueError("A JSON request body can only be sent with POST.")
        return self

    def display_url(self) -> str:
        return redact_url(self.url)


class FileSourceConfig(BaseModel):
    """Configuration of an uploaded file source."""

    model_config = ConfigDict(extra="forbid")

    stored_name: str = Field(min_length=1, max_length=200)   # relative to data/uploads
    original_name: str = Field(min_length=1, max_length=255)
    file_format: Literal["csv", "json", "parquet"]
    delimiter: str | None = Field(default=None, max_length=4)
    encoding: str = Field(default="utf-8", max_length=32)
    read_all_as_text: bool = False
    records_path: str | None = None
    flatten_nested: bool = True

    @field_validator("stored_name")
    @classmethod
    def _no_folders(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("Stored file names cannot contain folders.")
        return value


class SourceConfig(BaseModel):
    """A pipeline's data source: either an API or a stored file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["api", "file"]
    api: ApiSourceConfig | None = None
    file: FileSourceConfig | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "SourceConfig":
        if self.kind == "api" and self.api is None:
            raise ValueError("API settings are required for an API source.")
        if self.kind == "file" and self.file is None:
            raise ValueError("File settings are required for a file source.")
        return self

    def summary(self) -> str:
        if self.kind == "api" and self.api:
            return f"{self.api.method} {self.api.display_url()}"
        if self.file:
            return f"{self.file.file_format.upper()} file '{self.file.original_name}'"
        return self.kind
