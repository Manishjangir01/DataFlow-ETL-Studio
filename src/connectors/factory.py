"""Builds API extractors with the effective security policy and secret resolver."""

from __future__ import annotations

from typing import Callable, Iterable

import requests

from src.connectors.api_connector import ApiExtractor, build_session
from src.connectors.models import ApiSourceConfig
from src.security.secrets import SecretBox
from src.security.url_guard import Resolver, UrlPolicy, default_resolver
from src.utils.config import Settings


def effective_policy(
    settings: Settings,
    *,
    extra_domains: Iterable[str] = (),
    allow_http: bool | None = None,
    resolver: Resolver | None = None,
) -> UrlPolicy:
    """Combine the environment settings with the allow-list saved on the Settings page."""
    domains = tuple(sorted(set(settings.allowed_domains) | {d.strip().lower() for d in extra_domains if d.strip()}))
    return UrlPolicy(
        allow_http=settings.allow_http if allow_http is None else allow_http,
        allowed_domains=domains,
        resolver=resolver or default_resolver,
    )


def make_api_extractor_factory(
    settings: Settings,
    secret_box: SecretBox,
    policy: UrlPolicy,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Callable[[ApiSourceConfig], ApiExtractor]:
    """Return ``factory(api_config) -> ApiExtractor`` used by the UI and the runner."""

    def factory(config: ApiSourceConfig) -> ApiExtractor:
        kwargs = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return ApiExtractor(
            config,
            secret_resolver=secret_box.resolve,
            policy=policy,
            session=session or build_session(use_env_proxy=settings.use_env_proxy),
            log=log,
            max_response_bytes=settings.max_response_mb * 1_048_576,
            **kwargs,
        )

    return factory
