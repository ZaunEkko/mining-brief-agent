"""URL validation shared by tools that fetch caller-supplied URLs."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """The URL points somewhere a tool must not fetch."""


def ensure_public_url(url: str) -> None:
    """Basic SSRF guard: only absolute http(s) URLs to non-local hosts.

    This checks the URL text only; ``CachedHttp.check_public`` additionally checks the
    addresses a hostname resolves to, on every redirect hop.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeUrlError(f"only absolute http(s) URLs are allowed, got {url!r}")
    host = parts.hostname
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeUrlError(f"refusing to fetch local host {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not ip.is_global:
        raise UnsafeUrlError(f"refusing to fetch non-public address {host!r}")


async def resolve_host(host: str) -> list[str]:
    """All addresses ``host`` resolves to. DNS failures surface as connection errors."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectionError(f"cannot resolve {host!r}: {exc}") from exc
    return sorted({str(info[4][0]) for info in infos})
