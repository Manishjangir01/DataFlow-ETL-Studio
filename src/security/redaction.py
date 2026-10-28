"""Secret redaction for logs, error messages and anything shown in the UI.

Two complementary mechanisms are used:

1. **Known secrets** - every secret value that the application decrypts or reads from
   the environment is registered in a process-wide registry and replaced verbatim.
2. **Pattern rules** - credentials that look like secrets (``Authorization: Bearer``,
   ``?api_key=...``, ``password=...``, passwords inside database URLs) are masked even
   when the value was never registered.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***REDACTED***"
_MIN_SECRET_LENGTH = 4

# Normalised (lower-case, alphanumerics only) key names that always hold secrets.
_SENSITIVE_EXACT = {
    "password", "passwd", "pwd", "pass", "secret", "token", "apikey", "key", "auth",
    "authorization", "proxyauthorization", "cookie", "setcookie", "sessionid",
    "xapikey", "xauthtoken", "accesstoken", "refreshtoken", "idtoken", "clientsecret",
    "privatekey", "signature", "sig", "credentials", "credential",
}
_SENSITIVE_SUFFIXES = (
    "password", "passwd", "secret", "token", "apikey", "accesskey", "privatekey",
    "sessionid", "credential", "credentials",
)

_KEY_WORDS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|authorization|credentials?"
)
# key=value, key: value, "key": "value"  (the value may start with an auth scheme word)
_KV_PATTERN = re.compile(
    rf"(?i)(\b[\w.-]*(?:{_KEY_WORDS})[\w.-]*)(\"?'?\s*[=:]\s*\"?'?)"
    r"((?:(?:bearer|basic|token)\s+)?[^\s\"'&,;}\]]+)"
)
_BEARER_PATTERN = re.compile(r"\b([Bb]earer)(\s+)([A-Za-z0-9\-._~+/]{8,}=*)")
# scheme://user:password@host
_URL_PASSWORD_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s/]+)(@)")


def is_sensitive_key(key: str) -> bool:
    """Return True when a header / parameter / field name usually carries a secret."""
    normalised = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if not normalised:
        return False
    return normalised in _SENSITIVE_EXACT or normalised.endswith(_SENSITIVE_SUFFIXES)


class SecretRegistry:
    """Thread-safe set of secret values that must never appear in output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LENGTH:
            with self._lock:
                self._values.add(value)

    def values(self) -> list[str]:
        with self._lock:
            # Longest first so overlapping secrets are fully masked.
            return sorted(self._values, key=len, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_registry = SecretRegistry()


def register_secret(value: str | None) -> None:
    """Add a secret to the global registry so it is masked everywhere."""
    _registry.register(value)


def registered_secrets() -> list[str]:
    return _registry.values()


def clear_registered_secrets() -> None:
    """Forget all registered secrets (used by tests)."""
    _registry.clear()


def redact_text(text: Any, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with every known or recognisable secret masked."""
    if text is None:
        return ""
    result = str(text)
    secrets = [s for s in extra_secrets if s and len(s) >= _MIN_SECRET_LENGTH]
    for secret in sorted(set(secrets) | set(_registry.values()), key=len, reverse=True):
        result = result.replace(secret, MASK)
    result = _URL_PASSWORD_PATTERN.sub(rf"\1{MASK}\3", result)
    result = _BEARER_PATTERN.sub(rf"\1\2{MASK}", result)
    result = _KV_PATTERN.sub(
        lambda m: m.group(0) if m.group(3) == MASK or MASK in m.group(3) else f"{m.group(1)}{m.group(2)}{MASK}",
        result,
    )
    return result


def redact_url(url: str) -> str:
    """Mask passwords in the user-info part and sensitive query parameters of a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:{MASK}@{host}" if ":" in userinfo else f"{user}@{host}"
    query = urlencode(
        [(k, MASK if is_sensitive_key(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)],
        safe="*",
    )
    return redact_text(urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)))


def redact_mapping(data: Any) -> Any:
    """Recursively mask values stored under sensitive keys (headers, params, configs)."""
    if isinstance(data, Mapping):
        return {
            k: (MASK if is_sensitive_key(k) and v not in (None, "") else redact_mapping(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_mapping(v) for v in data]
    if isinstance(data, tuple):
        return tuple(redact_mapping(v) for v in data)
    if isinstance(data, str):
        return redact_text(data)
    return data


class RedactingFilter(logging.Filter):
    """Logging filter that masks secrets in every record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        if record.exc_info:
            # Render the traceback now so it can be redacted too.
            formatter = logging.Formatter()
            record.exc_text = redact_text(formatter.formatException(record.exc_info))
            record.exc_info = None
        return True
