"""Phase 2 tests: API extraction, pagination, authentication, retries and SSRF guard.

All HTTP traffic is mocked with ``responses`` - no live APIs are contacted.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest
import requests
import responses
from responses import matchers

from src.connectors.api_connector import ApiExtractor, build_session, parse_retry_after
from src.connectors.models import ApiSourceConfig, AuthConfig, AuthType, PaginationConfig, PaginationType
from src.security.redaction import redact_text
from src.security.secrets import SecretBox, SecretRef, generate_key
from src.security.url_guard import UrlPolicy, is_ip_allowed, validate_url
from src.utils.exceptions import ExtractionError, SecretError, SecurityError

BASE = "https://api.example.com/v1/items"


def public_resolver(host: str, port: int) -> list[str]:
    table = {
        "api.example.com": ["93.184.216.34"],
        "cdn.example.com": ["93.184.216.35"],
        "evil.example.com": ["127.0.0.1"],
        "mixed.example.com": ["93.184.216.34", "10.0.0.5"],
        "metadata.example.com": ["169.254.169.254"],
        "v6.example.com": ["::ffff:127.0.0.1"],
    }
    if host not in table:
        raise OSError("unknown host")
    return table[host]


POLICY = UrlPolicy(resolver=public_resolver)


def make_extractor(config: ApiSourceConfig, secret: str | None = None, sleeps: list | None = None) -> ApiExtractor:
    return ApiExtractor(
        config,
        secret_resolver=lambda ref: secret,
        policy=POLICY,
        session=build_session(),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def cfg(**kwargs) -> ApiSourceConfig:
    return ApiSourceConfig(url=kwargs.pop("url", BASE), authorized=True, **kwargs)


# --- URL guard ------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/api",
        "https://127.0.0.1/api",
        "https://10.1.2.3/x",
        "https://192.168.1.10/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/x",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://evil.example.com/x",          # DNS points to loopback
        "https://mixed.example.com/x",         # one private address is enough to block
        "https://metadata.example.com/x",
        "https://v6.example.com/x",            # IPv4-mapped IPv6 loopback
        "https://printer.local/x",
        "http://api.example.com/x",            # http disabled by default
        "ftp://api.example.com/x",
        "https://user:pass@api.example.com/x",
        "https://unknown-host.example.org/x",
    ],
)
def test_url_guard_blocks_restricted_destinations(url):
    with pytest.raises(SecurityError):
        validate_url(url, POLICY)


def test_url_guard_allows_public_and_respects_allowlist():
    assert validate_url(BASE, POLICY).addresses == ("93.184.216.34",)
    strict = UrlPolicy(allowed_domains=("example.org",), resolver=public_resolver)
    with pytest.raises(SecurityError):
        validate_url(BASE, strict)
    sub = UrlPolicy(allowed_domains=("example.com",), resolver=public_resolver)
    assert validate_url(BASE, sub).host == "api.example.com"
    http_ok = UrlPolicy(allow_http=True, resolver=public_resolver)
    assert validate_url("http://api.example.com/x", http_ok).port == 80


def test_ip_classification():
    assert is_ip_allowed("8.8.8.8")
    for ip in ("127.0.0.1", "10.0.0.1", "172.16.5.4", "100.64.0.1", "169.254.169.254", "0.0.0.0", "::1", "fd00::1", "224.0.0.1"):
        assert not is_ip_allowed(ip), ip


def test_socket_level_guard_blocks_rebinding():
    """Even if pre-validation were bypassed, the adapter refuses loopback sockets."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # pragma: no cover - must never be reached
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret internal data")

        def log_message(self, *args):  # pragma: no cover
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = build_session()
        with pytest.raises(requests.exceptions.ConnectionError) as info:
            session.get(f"http://127.0.0.1:{server.server_port}/", timeout=5)
        assert "blocked" in str(info.value)
    finally:
        server.shutdown()


# --- Basic extraction and response formats -------------------------------------

@responses.activate
def test_fetch_json_array():
    responses.get(BASE, json=[{"id": 1}, {"id": 2}])
    result = make_extractor(cfg()).fetch()
    assert [r["id"] for r in result.records] == [1, 2]
    assert result.pages_fetched == 1 and not result.truncated


@responses.activate
def test_fetch_nested_object_auto_detects_records():
    responses.get(BASE, json={"meta": {"total": 2}, "data": {"items": [{"id": 1}, {"id": 2}]}})
    result = make_extractor(cfg()).fetch()
    assert len(result.records) == 2
    assert result.records_path == "data.items"


@responses.activate
def test_fetch_with_explicit_records_path_and_probe():
    payload = {"results": [{"id": 1, "user": {"name": "a"}}], "other": [{"x": 1}]}
    responses.get(BASE, json=payload)
    responses.get(BASE, json=payload)
    extractor = make_extractor(cfg(records_path="other"))
    assert extractor.fetch().records == [{"x": 1}]
    probe = extractor.probe()
    assert probe.status_code == 200
    assert {path for _, path in probe.record_paths} == {"results", "other"}


@responses.activate
def test_post_with_json_body():
    responses.post(BASE, json=[{"ok": True}], match=[matchers.json_params_matcher({"query": "x"})])
    result = make_extractor(cfg(method="POST", json_body={"query": "x"})).fetch()
    assert result.records == [{"ok": True}]


def test_get_with_body_is_rejected():
    with pytest.raises(ValueError):
        cfg(method="GET", json_body={"a": 1})


# --- Pagination -----------------------------------------------------------------

@responses.activate
def test_page_number_pagination_stops_on_short_page():
    for page, rows in ((1, 2), (2, 2), (3, 1)):
        responses.get(
            BASE,
            json=[{"page": page, "n": i} for i in range(rows)],
            match=[matchers.query_param_matcher({"page": str(page), "per_page": "2"})],
        )
    config = cfg(pagination=PaginationConfig(type=PaginationType.PAGE_NUMBER, page_size=2))
    result = make_extractor(config).fetch()
    assert len(result.records) == 5 and result.pages_fetched == 3


@responses.activate
def test_offset_limit_pagination_stops_on_empty_page():
    responses.get(BASE, json={"rows": [{"i": 0}, {"i": 1}]}, match=[matchers.query_param_matcher({"offset": "0", "limit": "2"})])
    responses.get(BASE, json={"rows": [{"i": 2}, {"i": 3}]}, match=[matchers.query_param_matcher({"offset": "2", "limit": "2"})])
    responses.get(BASE, json={"rows": []}, match=[matchers.query_param_matcher({"offset": "4", "limit": "2"})])
    config = cfg(records_path="rows", pagination=PaginationConfig(type=PaginationType.OFFSET_LIMIT, page_size=2))
    result = make_extractor(config).fetch()
    assert [r["i"] for r in result.records] == [0, 1, 2, 3]


@responses.activate
def test_cursor_pagination():
    responses.get(BASE, json={"data": [{"i": 1}], "meta": {"next": "abc"}}, match=[matchers.query_param_matcher({})])
    responses.get(BASE, json={"data": [{"i": 2}], "meta": {"next": None}}, match=[matchers.query_param_matcher({"cursor": "abc"})])
    config = cfg(pagination=PaginationConfig(type=PaginationType.CURSOR, cursor_path="meta.next"))
    result = make_extractor(config).fetch()
    assert [r["i"] for r in result.records] == [1, 2]


@responses.activate
def test_cursor_loop_is_detected():
    responses.get(BASE, json={"data": [{"i": 1}], "next": "same"})
    config = cfg(pagination=PaginationConfig(type=PaginationType.CURSOR, cursor_path="next"))
    result = make_extractor(config).fetch()
    assert any("same cursor" in w for w in result.warnings)


@responses.activate
def test_next_link_in_body_and_link_header():
    responses.get(BASE, json={"items": [{"i": 1}], "next": BASE + "?page=2"}, match=[matchers.query_param_matcher({})])
    responses.get(BASE, json={"items": [{"i": 2}], "next": None}, match=[matchers.query_param_matcher({"page": "2"})])
    config = cfg(pagination=PaginationConfig(type=PaginationType.NEXT_LINK, next_link_path="next"))
    assert len(make_extractor(config).fetch().records) == 2

    responses.reset()
    responses.get(BASE, json=[{"i": 1}], headers={"Link": f'<{BASE}?page=2>; rel="next"'}, match=[matchers.query_param_matcher({})])
    responses.get(BASE, json=[{"i": 2}], match=[matchers.query_param_matcher({"page": "2"})])
    config = cfg(pagination=PaginationConfig(type=PaginationType.NEXT_LINK))
    assert len(make_extractor(config).fetch().records) == 2


@responses.activate
def test_next_link_to_other_host_is_blocked():
    responses.get(BASE, json={"items": [{"i": 1}], "next": "https://cdn.example.com/page2"})
    config = cfg(pagination=PaginationConfig(type=PaginationType.NEXT_LINK, next_link_path="next"))
    with pytest.raises(SecurityError):
        make_extractor(config).fetch()


@responses.activate
def test_max_records_and_max_pages_limits():
    responses.get(BASE, json=[{"i": i} for i in range(10)])
    result = make_extractor(cfg(max_records=4)).fetch()
    assert len(result.records) == 4 and result.truncated

    responses.reset()
    responses.get(BASE, json=[{"i": 1}, {"i": 2}])
    config = cfg(pagination=PaginationConfig(type=PaginationType.PAGE_NUMBER, page_size=2, max_pages=3))
    result = make_extractor(config).fetch()
    assert result.pages_fetched == 3 and result.truncated
    assert any("maximum of 3 pages" in w for w in result.warnings)


# --- Authentication ----------------------------------------------------------------

@responses.activate
def test_bearer_token_header():
    responses.get(BASE, json=[{"a": 1}], match=[matchers.header_matcher({"Authorization": "Bearer tok_12345678"})])
    config = cfg(auth=AuthConfig(type=AuthType.BEARER, secret={"kind": "env", "value": "TOKEN"}))
    assert make_extractor(config, secret="tok_12345678").fetch().records == [{"a": 1}]


@responses.activate
def test_api_key_header_and_query():
    responses.get(BASE, json=[{"a": 1}], match=[matchers.header_matcher({"X-API-Key": "key-abcdef"})])
    config = cfg(auth=AuthConfig(type=AuthType.API_KEY, secret={"kind": "env", "value": "K"}))
    assert make_extractor(config, secret="key-abcdef").fetch().records

    responses.reset()
    responses.get(BASE, json=[{"a": 1}], match=[matchers.query_param_matcher({"appid": "key-abcdef"})])
    config = cfg(auth=AuthConfig(type=AuthType.API_KEY, api_key_name="appid", api_key_location="query", secret={"kind": "env", "value": "K"}))
    assert make_extractor(config, secret="key-abcdef").fetch().records


def test_auth_secret_required_and_credentials_not_allowed_in_headers():
    with pytest.raises(ValueError):
        cfg(headers={"Authorization": "Bearer abc"})
    with pytest.raises(ValueError):
        cfg(query_params={"api_key": "abc"})
    config = cfg(auth=AuthConfig(type=AuthType.BEARER))
    with pytest.raises(ExtractionError):
        make_extractor(config).fetch()


def test_secret_box_roundtrip_and_env_refs(monkeypatch):
    box = SecretBox(generate_key())
    ref = box.encrypt("my-api-key-123")
    assert "my-api-key-123" not in ref.value
    assert box.resolve(ref) == "my-api-key-123"
    monkeypatch.setenv("DF_TEST_TOKEN", "env-token-xyz")
    assert box.resolve(SecretRef(kind="env", value="DF_TEST_TOKEN")) == "env-token-xyz"
    monkeypatch.delenv("DF_TEST_TOKEN")
    with pytest.raises(SecretError):
        box.resolve(SecretRef(kind="env", value="DF_TEST_TOKEN"))
    with pytest.raises(SecretError):
        SecretBox(generate_key()).decrypt(ref.value)  # wrong key


@responses.activate
def test_secret_never_appears_in_error_messages():
    responses.get(BASE, status=401, body="invalid token tok_12345678 for user")
    config = cfg(auth=AuthConfig(type=AuthType.BEARER, secret={"kind": "env", "value": "T"}))
    extractor = ApiExtractor(
        config, secret_resolver=lambda ref: "tok_12345678", policy=POLICY, session=build_session(), sleep=lambda s: None
    )
    # Mirror production behaviour: resolved secrets are registered for redaction.
    from src.security.redaction import register_secret
    register_secret("tok_12345678")
    with pytest.raises(ExtractionError) as info:
        extractor.fetch()
    assert info.value.status_code == 401
    assert "tok_12345678" not in str(info.value)
    assert "tok_12345678" not in redact_text(info.value.message)


# --- Errors, retries and rate limits ------------------------------------------------

@responses.activate
def test_rate_limit_retry_after_is_honoured():
    responses.get(BASE, status=429, headers={"Retry-After": "7"})
    responses.get(BASE, json=[{"ok": 1}])
    sleeps: list[float] = []
    result = make_extractor(cfg(), sleeps=sleeps).fetch()
    assert result.records == [{"ok": 1}]
    assert sleeps == [7.0]
    assert result.requests_made == 1


@responses.activate
def test_server_errors_retry_then_fail():
    for _ in range(3):
        responses.get(BASE, status=503)
    sleeps: list[float] = []
    with pytest.raises(ExtractionError) as info:
        make_extractor(cfg(max_retries=2), sleeps=sleeps).fetch()
    assert info.value.status_code == 503
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_timeout_is_retried_then_reported():
    responses.get(BASE, body=requests.exceptions.ReadTimeout("slow"))
    responses.get(BASE, body=requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(ExtractionError) as info:
        make_extractor(cfg(max_retries=1)).fetch()
    assert "did not respond" in info.value.message


@responses.activate
@pytest.mark.parametrize(
    "body, content_type, fragment",
    [
        ("<html>Login</html>", "text/html", "not valid JSON"),
        ("", "application/json", "empty response"),
        ('{"broken": ', "application/json", "not valid JSON"),
    ],
)
def test_malformed_responses(body, content_type, fragment):
    responses.get(BASE, body=body, content_type=content_type)
    with pytest.raises(ExtractionError) as info:
        make_extractor(cfg()).fetch()
    assert fragment in info.value.message


@responses.activate
def test_not_found_and_bad_records_path():
    responses.get(BASE, status=404, json={"error": "nope"})
    with pytest.raises(ExtractionError) as info:
        make_extractor(cfg()).fetch()
    assert info.value.status_code == 404 and "URL path" in (info.value.hint or "")
    responses.reset()
    responses.get(BASE, json={"data": [{"a": 1}]})
    with pytest.raises(ExtractionError):
        make_extractor(cfg(records_path="results")).fetch()


@responses.activate
def test_redirects_same_host_followed_other_host_blocked():
    responses.get(BASE, status=302, headers={"Location": "/v2/items"})
    responses.get("https://api.example.com/v2/items", json=[{"v": 2}])
    assert make_extractor(cfg()).fetch().records == [{"v": 2}]
    responses.reset()
    responses.get(BASE, status=302, headers={"Location": "https://evil.example.com/steal"})
    with pytest.raises(SecurityError):
        make_extractor(cfg()).fetch()


@responses.activate
def test_response_size_limit():
    responses.get(BASE, body=json.dumps([{"x": "a" * 2000}]), content_type="application/json")
    extractor = make_extractor(cfg())
    extractor._max_bytes = 100
    with pytest.raises(ExtractionError) as info:
        extractor.fetch()
    assert "larger than" in info.value.message


def test_parse_retry_after_formats():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
