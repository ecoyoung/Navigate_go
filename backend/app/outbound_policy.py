import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit


class UnsafeOutboundURLError(ValueError):
    """An outbound target is not allowed by the public-network policy."""


Resolver = Callable[[str, int], list[str]]

BLOCKED_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.google.internal.",
}


def _default_resolver(host: str, port: int) -> list[str]:
    addresses: list[str] = []
    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(item[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def validate_public_http_url(
    value: str,
    *,
    resolver: Resolver | None = None,
    resolve_dns: bool = True,
) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeOutboundURLError("unsupported_url_scheme")
    if not parts.hostname:
        raise UnsafeOutboundURLError("missing_url_host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeOutboundURLError("url_userinfo_not_allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeOutboundURLError("invalid_url_port") from exc
    expected_port = 443 if parts.scheme.lower() == "https" else 80
    if port is not None and port != expected_port:
        raise UnsafeOutboundURLError("non_standard_port_not_allowed")

    host = parts.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTS or any(host.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES):
        raise UnsafeOutboundURLError("non_public_host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeOutboundURLError("non_public_ip")

    if resolve_dns and literal is None:
        try:
            addresses = (resolver or _default_resolver)(host, port or expected_port)
        except OSError as exc:
            raise UnsafeOutboundURLError("dns_resolution_failed") from exc
        if not addresses:
            raise UnsafeOutboundURLError("dns_no_addresses")
        for address in addresses:
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeOutboundURLError("dns_invalid_address") from exc
            if not resolved.is_global:
                raise UnsafeOutboundURLError("dns_non_public_address")
    return value.strip()
