"""Outbound URL policy: blocks SSRF to localhost, private networks and cloud metadata.

Protection happens at two levels:

1. :func:`validate_url` checks the scheme, host name, optional domain allow-list and
   every IP address the host resolves to *before* a request is made.
2. :class:`GuardedHTTPAdapter` re-checks the IP address on every new socket
   connection, so DNS rebinding (a host that resolves to a public IP during validation
   and to 127.0.0.1 a moment later) and redirects cannot reach internal addresses.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util import connection as urllib3_connection

from src.utils.exceptions import SecurityError

Resolver = Callable[[str, int], list[str]]

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".svc", ".cluster.local")
# Explicitly listed for clarity; all of them are also rejected by the is_global check.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
        "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
    )
)
BLOCK_MESSAGE = "DataFlow blocked a connection to a restricted address"


def default_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` to all of its IP addresses."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0].split("%", 1)[0] for info in infos})


def is_ip_allowed(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, ip.teredo[1] if ip.teredo else None):
            if embedded is not None and not is_ip_allowed(embedded):
                return False
    if any(ip in network for network in BLOCKED_NETWORKS if network.version == ip.version):
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


@dataclass(frozen=True)
class UrlPolicy:
    """Rules for which URLs API sources may call."""

    allow_http: bool = False
    allowed_domains: tuple[str, ...] = ()
    resolver: Resolver = field(default=default_resolver, compare=False)


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def validate_url(url: str, policy: UrlPolicy) -> ValidatedUrl:
    """Validate ``url`` against ``policy``.

    Raises:
        SecurityError: when the URL is malformed or points to a restricted destination.
    """
    if not url or len(url) > 2048:
        raise SecurityError("The URL is empty or longer than 2048 characters.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise SecurityError(f"The URL is not valid: {exc}") from exc

    scheme = parts.scheme.lower()
    allowed_schemes = ("https", "http") if policy.allow_http else ("https",)
    if scheme not in allowed_schemes:
        hint = None if scheme != "http" else "Plain http is disabled. Enable it in Settings if you must."
        raise SecurityError(f"URL scheme '{scheme or '(none)'}' is not allowed. Use https://.", hint=hint)
    if parts.username or parts.password:
        raise SecurityError(
            "Credentials inside the URL are not allowed.",
            hint="Use the Authentication settings so the secret is encrypted.",
        )
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise SecurityError("The URL has no host name.")
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SecurityError(f"Invalid host name '{host}'.") from exc
    port = port or (443 if scheme == "https" else 80)

    if host_ascii in BLOCKED_HOSTNAMES or host_ascii.endswith(BLOCKED_SUFFIXES):
        raise SecurityError(f"Requests to '{host_ascii}' are blocked (local or internal host).")
    if policy.allowed_domains and not _host_matches(host_ascii, policy.allowed_domains):
        raise SecurityError(
            f"'{host_ascii}' is not in the list of allowed API domains.",
            hint="An administrator can add it under Settings > API security.",
        )

    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(host_ascii.strip("[]"))
    except ValueError:
        pass
    if literal_ip is not None:
        addresses = [str(literal_ip)]
    else:
        try:
            addresses = policy.resolver(host_ascii, port)
        except (socket.gaierror, OSError) as exc:
            raise SecurityError(
                f"The host '{host_ascii}' could not be resolved.",
                hint="Check the URL spelling and your internet connection.",
            ) from exc
    if not addresses:
        raise SecurityError(f"The host '{host_ascii}' did not resolve to any address.")
    blocked = [a for a in addresses if not is_ip_allowed(a)]
    if blocked:
        raise SecurityError(
            f"'{host_ascii}' resolves to a private, loopback, link-local or metadata address "
            "and is blocked.",
            hint="Only public internet APIs can be used as sources.",
        )
    return ValidatedUrl(url=url.strip(), scheme=scheme, host=host_ascii, port=port, addresses=tuple(addresses))


# ---------------------------------------------------------------------------------
# Socket-level enforcement
# ---------------------------------------------------------------------------------

class _GuardedConnectionMixin:
    """Resolves the host itself and refuses to connect to non-public addresses."""

    def _new_conn(self):  # type: ignore[override]
        host = self._dns_host  # type: ignore[attr-defined]
        port = self.port  # type: ignore[attr-defined]
        try:
            addresses = default_resolver(host, port)
        except OSError as exc:
            raise NewConnectionError(self, f"Failed to resolve '{host}': {exc}") from exc  # type: ignore[arg-type]
        if not addresses or any(not is_ip_allowed(a) for a in addresses):
            raise NewConnectionError(self, f"{BLOCK_MESSAGE} ({host}).")  # type: ignore[arg-type]
        last_error: OSError | None = None
        for address in addresses:
            try:
                return urllib3_connection.create_connection(
                    (address, port),
                    self.timeout,  # type: ignore[attr-defined]
                    source_address=self.source_address,  # type: ignore[attr-defined]
                    socket_options=self.socket_options,  # type: ignore[attr-defined]
                )
            except OSError as exc:
                last_error = exc
        raise NewConnectionError(self, f"Failed to connect to {host}: {last_error}")  # type: ignore[arg-type]


class GuardedHTTPConnection(_GuardedConnectionMixin, HTTPConnection):
    pass


class GuardedHTTPSConnection(_GuardedConnectionMixin, HTTPSConnection):
    pass


class GuardedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = GuardedHTTPConnection


class GuardedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = GuardedHTTPSConnection


class GuardedHTTPAdapter(HTTPAdapter):
    """requests adapter whose direct connections are checked against the IP policy."""

    def init_poolmanager(self, *args, **kwargs):  # type: ignore[override]
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": GuardedHTTPConnectionPool,
            "https": GuardedHTTPSConnectionPool,
        }
