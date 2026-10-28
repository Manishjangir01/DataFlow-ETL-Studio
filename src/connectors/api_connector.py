"""REST API extraction with authentication, pagination, retries and SSRF protection."""

from __future__ import annotations

import email.utils
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from src.connectors.models import ApiSourceConfig, AuthType, PaginationType
from src.extraction.normalize import extract_records, find_record_paths, get_path
from src.security.redaction import redact_text, redact_url
from src.security.secrets import SecretRef
from src.security.url_guard import BLOCK_MESSAGE, GuardedHTTPAdapter, UrlPolicy, validate_url
from src.utils.exceptions import ExtractionError, SecurityError
from src.utils.logging_setup import get_logger

RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 120
USER_AGENT = "DataFlow-ETL-Studio/1.0"

_log = get_logger("connectors.api")

SecretResolver = Callable[[SecretRef | None], str | None]
LogCallback = Callable[[str], None]


@dataclass
class FetchResult:
    records: list[dict[str, Any]]
    pages_fetched: int = 0
    requests_made: int = 0
    truncated: bool = False
    records_path: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    """First page of a response, used by 'Test connection'."""

    status_code: int
    elapsed_ms: int
    content_type: str
    payload: Any
    record_paths: list[tuple[str, str]]   # (label, path)
    record_count: int
    sample_records: list[dict[str, Any]]


def build_session(*, use_env_proxy: bool = False) -> requests.Session:
    """Create a requests session whose connections are IP-checked."""
    session = requests.Session()
    session.trust_env = use_env_proxy
    adapter = GuardedHTTPAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return max(delta, 0.0)


class ApiExtractor:
    """Fetches all records from a configured REST API."""

    def __init__(
        self,
        config: ApiSourceConfig,
        *,
        secret_resolver: SecretResolver,
        policy: UrlPolicy,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: LogCallback | None = None,
        max_response_bytes: int = 50 * 1_048_576,
    ) -> None:
        self.config = config
        self.policy = policy
        self.session = session or build_session()
        self._sleep = sleep
        self._log_callback = log
        self._max_bytes = max_response_bytes
        self._secret: str | None = None
        self._secret_loaded = False
        self._resolve_secret = secret_resolver
        self._base = validate_url(config.url, policy)

    # -- helpers -----------------------------------------------------------------
    def _log(self, message: str) -> None:
        clean = redact_text(message, [self._secret] if self._secret else [])
        _log.info(clean)
        if self._log_callback:
            self._log_callback(clean)

    def _get_secret(self) -> str | None:
        if not self._secret_loaded:
            auth = self.config.auth
            if auth.type != AuthType.NONE:
                if auth.secret is None:
                    raise ExtractionError(
                        f"Authentication is set to {auth.type.value} but no secret is saved.",
                        hint="Enter the API key or token in the source settings.",
                    )
                self._secret = self._resolve_secret(auth.secret)
            self._secret_loaded = True
        return self._secret

    def _auth_headers_and_params(self) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        secret = self._get_secret()
        if auth.type == AuthType.BEARER and secret:
            return {"Authorization": f"Bearer {secret}"}, {}
        if auth.type == AuthType.API_KEY and secret:
            if auth.api_key_location == "query":
                return {}, {auth.api_key_name: secret}
            return {auth.api_key_name: secret}, {}
        return {}, {}

    def _same_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return (parts.hostname or "").lower() == self._base.host and port == self._base.port

    # -- HTTP --------------------------------------------------------------------
    def _send(self, url: str, params: dict[str, str] | None) -> requests.Response:
        """Send one request, following safe redirects and retrying transient errors."""
        cfg = self.config
        attempt = 0
        current_url = url
        current_params = params
        redirects = 0
        while True:
            validate_url(current_url, self.policy)
            if not self._same_origin(current_url):
                raise SecurityError(
                    f"The API tried to send the request to another host ({urlsplit(current_url).hostname}).",
                    hint="For safety, credentials are only sent to the configured host.",
                )
            auth_headers, auth_params = self._auth_headers_and_params()
            headers = {**cfg.headers, **auth_headers}
            all_params = {**(current_params or {}), **auth_params}
            try:
                response = self.session.request(
                    cfg.method,
                    current_url,
                    params=all_params or None,
                    headers=headers,
                    json=cfg.json_body if cfg.method == "POST" else None,
                    timeout=(min(cfg.timeout_seconds, 15.0), cfg.timeout_seconds),
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.ConnectionError as exc:
                if BLOCK_MESSAGE in str(exc):
                    raise SecurityError(
                        "The connection was blocked because the host resolved to a restricted address.",
                    ) from exc
                if attempt < cfg.max_retries:
                    attempt += 1
                    delay = self._backoff(attempt)
                    self._log(f"Connection error, retry {attempt}/{cfg.max_retries} in {delay:.1f}s")
                    self._sleep(delay)
                    continue
                raise ExtractionError(
                    f"Could not connect to {self._base.host}: {redact_text(type(exc).__name__)}.",
                    hint="Check the URL and your network connection.",
                ) from exc
            except requests.exceptions.Timeout as exc:
                if attempt < cfg.max_retries:
                    attempt += 1
                    delay = self._backoff(attempt)
                    self._log(f"Request timed out, retry {attempt}/{cfg.max_retries} in {delay:.1f}s")
                    self._sleep(delay)
                    continue
                raise ExtractionError(
                    f"The API did not respond within {cfg.timeout_seconds:.0f} seconds.",
                    hint="Increase the request timeout or try again later.",
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise ExtractionError(f"Request failed: {redact_text(str(exc))}") from exc

            if response.status_code in REDIRECT_STATUS:
                location = response.headers.get("Location")
                response.close()
                if not location or redirects >= MAX_REDIRECTS:
                    raise ExtractionError("Too many redirects or a redirect without a location.")
                redirects += 1
                current_url = urljoin(current_url, location)
                current_params = None  # parameters are part of the redirect target
                self._log(f"Following redirect to {redact_url(current_url)}")
                continue

            if response.status_code in RETRY_STATUS and attempt < cfg.max_retries:
                attempt += 1
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                delay = min(retry_after, MAX_RETRY_AFTER_SECONDS) if retry_after is not None else self._backoff(attempt)
                reason = "Rate limited (HTTP 429)" if response.status_code == 429 else f"HTTP {response.status_code}"
                self._log(f"{reason}; retry {attempt}/{cfg.max_retries} in {delay:.1f}s")
                response.close()
                self._sleep(delay)
                continue
            return response

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(2 ** (attempt - 1), 30))

    def _read_json(self, response: requests.Response) -> Any:
        status = response.status_code
        body = self._read_body(response)
        if status >= 400:
            snippet = redact_text(body[:300].decode("utf-8", "replace"), [self._secret or ""])
            hints = {
                401: "Check the API key / token.",
                403: "The credentials do not have permission for this resource.",
                404: "Check the URL path.",
                429: "The API rate limit was exceeded; try again later or reduce max pages.",
            }
            raise ExtractionError(
                f"The API returned HTTP {status} {response.reason or ''}. Response: {snippet}".strip(),
                hint=hints.get(status, "Check the request settings." if status < 500 else "The API server had an error; try again later."),
                status_code=status,
            )
        if not body.strip():
            raise ExtractionError("The API returned an empty response.", status_code=status)
        try:
            return json.loads(body.decode(response.encoding or "utf-8", "replace"))
        except (json.JSONDecodeError, LookupError) as exc:
            content_type = response.headers.get("Content-Type", "unknown")
            snippet = redact_text(body[:120].decode("utf-8", "replace"))
            raise ExtractionError(
                f"The response is not valid JSON (content type: {content_type}). It starts with: {snippet!r}",
                hint="Make sure the URL points to a JSON API endpoint.",
                status_code=status,
            ) from exc

    def _read_body(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._max_bytes:
                    raise ExtractionError(
                        f"The response is larger than {self._max_bytes / 1_048_576:.0f} MB.",
                        hint="Use pagination with a smaller page size or raise DATAFLOW_MAX_RESPONSE_MB.",
                    )
                chunks.append(chunk)
        except requests.exceptions.RequestException as exc:
            raise ExtractionError(f"The connection broke while reading the response: {type(exc).__name__}") from exc
        finally:
            response.close()
        return b"".join(chunks)

    # -- Public API --------------------------------------------------------------
    def probe(self) -> ProbeResult:
        """Fetch only the first page so the user can inspect the response."""
        started = time.monotonic()
        response = self._send(self.config.url, self._page_params(0, None))
        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        payload = self._read_json(response)
        elapsed = int((time.monotonic() - started) * 1000)
        if isinstance(payload, list):
            paths = [("(root) - the response is a list", "")]
        else:
            paths = [(c.label, c.path) for c in find_record_paths(payload)]
        records = extract_records(payload, self.config.records_path)
        return ProbeResult(status, elapsed, content_type, payload, paths, len(records), records[:20])

    def _page_params(self, page_index: int, cursor: str | None) -> dict[str, str]:
        p = self.config.pagination
        params = dict(self.config.query_params)
        if p.type == PaginationType.PAGE_NUMBER:
            params[p.page_param] = str(p.start_page + page_index)
            if p.size_param:
                params[p.size_param] = str(p.page_size)
        elif p.type == PaginationType.OFFSET_LIMIT:
            params[p.offset_param] = str(page_index * p.page_size)
            params[p.limit_param] = str(p.page_size)
        elif p.type == PaginationType.CURSOR and cursor:
            params[p.cursor_param] = cursor
        return params

    def fetch(self, *, max_records: int | None = None, max_pages: int | None = None) -> FetchResult:
        """Fetch every page until the data ends or a limit is reached."""
        cfg = self.config
        pagination = cfg.pagination
        record_limit = min(cfg.max_records, max_records) if max_records else cfg.max_records
        page_limit = 1 if pagination.type == PaginationType.NONE else pagination.max_pages
        if max_pages:
            page_limit = min(page_limit, max_pages)

        result = FetchResult(records=[], records_path=cfg.records_path)
        url = cfg.url
        cursor: str | None = None
        seen_urls: set[str] = set()
        seen_cursors: set[str] = set()
        page_index = 0
        last_payload: Any = None
        last_link: str | None = None
        last_records: list[dict[str, Any]] = []

        while page_index < page_limit:
            is_follow_link = pagination.type == PaginationType.NEXT_LINK and page_index > 0
            params = None if is_follow_link else self._page_params(page_index, cursor)
            response = self._send(url, params)
            result.requests_made += 1
            link_next = response.links.get("next", {}).get("url") if response.links else None
            payload = self._read_json(response)
            records = extract_records(payload, cfg.records_path)
            last_payload, last_link, last_records = payload, link_next, records
            if page_index == 0 and cfg.records_path is None and isinstance(payload, dict):
                candidates = find_record_paths(payload)
                result.records_path = candidates[0].path if candidates else None
            page_index += 1
            result.pages_fetched = page_index
            self._log(f"Page {page_index}: {len(records)} records")

            if not records and pagination.stop_on_empty_page:
                break
            remaining = record_limit - len(result.records)
            if len(records) >= remaining:
                result.records.extend(records[:remaining])
                if len(records) > remaining or self._has_more(pagination.type, payload, link_next, records):
                    result.truncated = True
                    result.warnings.append(f"Stopped at the maximum of {record_limit:,} records per run.")
                break
            result.records.extend(records)

            # Decide the next page.
            if pagination.type == PaginationType.NONE:
                break
            if pagination.type in (PaginationType.PAGE_NUMBER, PaginationType.OFFSET_LIMIT):
                if len(records) < pagination.page_size:
                    break
                continue
            if pagination.type == PaginationType.CURSOR:
                next_cursor = self._lookup(payload, pagination.cursor_path)
                if next_cursor in (None, "", False):
                    break
                next_cursor = str(next_cursor)
                if next_cursor in seen_cursors:
                    result.warnings.append("The API returned the same cursor twice; stopped to avoid a loop.")
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                continue
            if pagination.type == PaginationType.NEXT_LINK:
                next_url = self._lookup(payload, pagination.next_link_path) if pagination.next_link_path else link_next
                if not next_url:
                    break
                next_url = urljoin(url, str(next_url))
                if next_url in seen_urls or next_url == url:
                    result.warnings.append("The API returned the same next link twice; stopped to avoid a loop.")
                    break
                seen_urls.add(url)
                url = next_url
                continue
        else:
            if page_index > 0 and self._has_more(pagination.type, last_payload, last_link, last_records):
                result.warnings.append(
                    f"Stopped after the maximum of {page_limit} pages; more data is available."
                )
                result.truncated = True

        self._log(
            f"Extraction finished: {len(result.records)} records, {result.pages_fetched} pages, "
            f"{result.requests_made} requests"
        )
        return result

    def _has_more(self, ptype: PaginationType, payload: Any, link_next: str | None, records: list) -> bool:
        p = self.config.pagination
        if ptype == PaginationType.NONE:
            return False
        if ptype in (PaginationType.PAGE_NUMBER, PaginationType.OFFSET_LIMIT):
            return len(records) >= p.page_size
        if ptype == PaginationType.CURSOR:
            return self._lookup(payload, p.cursor_path) not in (None, "", False)
        next_url = self._lookup(payload, p.next_link_path) if p.next_link_path else link_next
        return bool(next_url)

    @staticmethod
    def _lookup(payload: Any, path: str | None) -> Any:
        if not path:
            return None
        try:
            return get_path(payload, path)
        except ExtractionError:
            return None
