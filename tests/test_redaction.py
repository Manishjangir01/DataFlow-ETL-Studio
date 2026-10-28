"""Secret redaction tests: secrets must never reach logs, errors or the UI."""

from __future__ import annotations

import logging

from src.security.redaction import (
    MASK,
    RedactingFilter,
    is_sensitive_key,
    redact_mapping,
    redact_text,
    redact_url,
    register_secret,
)
from src.utils.logging_setup import RunLogger


def test_registered_secret_is_masked_everywhere():
    register_secret("sk_live_SUPERSECRET123")
    text = "calling with key sk_live_SUPERSECRET123 failed"
    assert "SUPERSECRET" not in redact_text(text)
    assert MASK in redact_text(text)


def test_pattern_based_redaction():
    samples = [
        "Authorization: Bearer abcdef1234567890",
        "GET https://api.example.com/v1?api_key=abc123xyz&page=2",
        '{"password": "hunter2!!", "user": "bob"}',
        "postgresql+psycopg://etl:Pa55word@db:5432/warehouse",
        "client_secret=s3cr3tvalue",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    ]
    secrets = ["abcdef1234567890", "abc123xyz", "hunter2!!", "Pa55word", "s3cr3tvalue", "dXNlcjpwYXNzd29yZA=="]
    for sample, secret in zip(samples, secrets):
        assert secret not in redact_text(sample), sample


def test_redaction_keeps_normal_text():
    text = "Extracted 120 records; basic data quality report ready; Authentication type: Bearer Token"
    assert redact_text(text) == text


def test_redact_url_and_mapping():
    url = redact_url("https://user:pw123@api.example.com/x?token=t0k3n&page=1")
    assert "pw123" not in url and "t0k3n" not in url and "page=1" in url
    mapping = redact_mapping({"Authorization": "Bearer x", "X-Api-Key": "k", "Accept": "json", "nested": {"password": "p"}})
    assert mapping["Authorization"] == MASK and mapping["X-Api-Key"] == MASK
    assert mapping["Accept"] == "json" and mapping["nested"]["password"] == MASK


def test_sensitive_key_detection():
    assert is_sensitive_key("X-API-KEY") and is_sensitive_key("access_token") and is_sensitive_key("key")
    assert not is_sensitive_key("author") and not is_sensitive_key("page") and not is_sensitive_key("monkey_count")


def test_logging_filter_redacts_messages_and_args(caplog):
    register_secret("tok_9876543210")
    logger = logging.getLogger("dataflow.test.redaction")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="dataflow.test.redaction"):
        logger.info("token is %s", "tok_9876543210")
    assert "tok_9876543210" not in caplog.text
    assert MASK in caplog.text


def test_run_logger_redacts_per_run_secret():
    run_logger = RunLogger("run#1", extra_secrets=["per-run-secret-value"])
    run_logger.info("extract", "header X-Key: per-run-secret-value sent")
    assert "per-run-secret-value" not in run_logger.text
