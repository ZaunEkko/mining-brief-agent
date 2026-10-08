"""Cached HTTP fetching with an explicit fallback chain.

Resolution order for ``CachedHttp.get``:

1. fresh cache entry (``DataMode.CACHE``)
2. live request (``DataMode.LIVE``), written back to the cache
3. stale cache entry when the live request fails (``DataMode.STALE_CACHE``)
4. recorded fixture (``DataMode.FIXTURE``)

With ``offline=True`` step 2 is skipped. Cache and fixtures share one on-disk layout
(``<sha256(url)>.bin`` + ``.json`` metadata), so recording fixtures is just fetching
with the fixtures directory as the cache.

Live responses are streamed with a byte limit and never buffered past it. For
caller-supplied URLs (``public_only=True``) redirects are followed by hand and every
hop is checked, including the addresses its hostname resolves to (SSRF guard).
"""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel

from mining_brief.common.config import Settings
from mining_brief.common.models import DataMode
from mining_brief.common.urls import UnsafeUrlError, ensure_public_url, resolve_host

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 2
RETRY_BACKOFF_S = 1.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

Resolver = Callable[[str], Awaitable[list[str]]]


class FetchError(Exception):
    """Raised when no live response, cache entry or fixture is available."""


class ResponseTooLarge(FetchError):
    """The upstream response exceeded the caller's byte limit."""


class FetchResult(BaseModel):
    url: str
    content: bytes
    content_type: str | None
    retrieved_at: datetime
    data_mode: DataMode

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class _Entry(BaseModel):
    url: str
    content_type: str | None
    retrieved_at: datetime


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class DiskStore:
    """``<key>.bin`` + ``<key>.json`` pairs under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, url: str) -> tuple[_Entry, bytes] | None:
        key = cache_key(url)
        meta, body = self.root / f"{key}.json", self.root / f"{key}.bin"
        if not (meta.is_file() and body.is_file()):
            return None
        return _Entry.model_validate_json(meta.read_text("utf-8")), body.read_bytes()

    def write(
        self,
        url: str,
        content: bytes,
        content_type: str | None,
        retrieved_at: datetime | None = None,
    ) -> _Entry:
        self.root.mkdir(parents=True, exist_ok=True)
        key = cache_key(url)
        entry = _Entry(
            url=url, content_type=content_type, retrieved_at=retrieved_at or datetime.now(UTC)
        )
        (self.root / f"{key}.bin").write_bytes(content)
        (self.root / f"{key}.json").write_text(
            entry.model_dump_json(indent=2) + "\n", "utf-8", newline="\n"
        )
        return entry


class CachedHttp:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_s,
            follow_redirects=False,  # followed by hand so every hop can be checked
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            },
        )
        self._resolver = resolver or resolve_host
        self._allowed_networks = [ip_network(n) for n in settings.ssrf_allowed_networks]
        self._cache = DiskStore(settings.cache_dir / "http")
        self._fixtures = DiskStore(settings.fixtures_dir / "http")
        # Requests to one host are serialised to stay polite to upstream sites.
        self._host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_public(self, url: str) -> None:
        """Refuse URLs that are, or resolve to, non-public addresses."""
        ensure_public_url(url)
        host = urlsplit(url).hostname or ""
        try:
            ip_address(host)
            return  # literal IPs were fully checked by ensure_public_url
        except ValueError:
            pass
        for address in await self._resolver(host):
            ip = ip_address(address)
            if not ip.is_global and not any(ip in net for net in self._allowed_networks):
                raise UnsafeUrlError(f"{host!r} resolves to non-public address {address}")

    async def get(
        self,
        url: str,
        *,
        ttl: timedelta,
        public_only: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> FetchResult:
        if public_only:
            ensure_public_url(url)
        cached = self._cache.read(url)
        now = datetime.now(UTC)
        if cached and now - cached[0].retrieved_at < ttl:
            return _result(cached, DataMode.CACHE)

        if not self._settings.offline:
            try:
                return await self._fetch_live(url, public_only, max_bytes)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning("live fetch failed for %s: %r", url, exc)

        if cached:
            return _result(cached, DataMode.STALE_CACHE)
        fixture = self._fixtures.read(url)
        if fixture:
            return _result(fixture, DataMode.FIXTURE)
        mode = "offline mode" if self._settings.offline else "upstream unreachable"
        raise FetchError(f"{mode} and no cached copy or fixture for {url}")

    async def _fetch_live(self, url: str, public_only: bool, max_bytes: int) -> FetchResult:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if public_only:
                await self.check_public(current)
            async with self._host_locks[urlsplit(current).netloc]:
                response, content = await self._get_with_retry(current, max_bytes)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            entry = self._cache.write(url, content, content_type)
            return _result((entry, content), DataMode.LIVE)
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects for {url}")

    async def _get_with_retry(self, url: str, max_bytes: int) -> tuple[httpx.Response, bytes]:
        """Stream one response within ``max_bytes``; one retry for transient failures."""
        for attempt in range(RETRY_ATTEMPTS):
            last = attempt == RETRY_ATTEMPTS - 1
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code in RETRY_STATUSES and not last:
                        pass  # fall through to the backoff below
                    else:
                        return response, await _read_limited(response, url, max_bytes)
            except httpx.TransportError:
                if last:
                    raise
            await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
        raise AssertionError("unreachable")


async def _read_limited(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(f"{url} is {int(declared)} bytes; limit is {max_bytes}")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLarge(f"{url} exceeded the {max_bytes}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _result(item: tuple[_Entry, bytes], mode: DataMode) -> FetchResult:
    entry, content = item
    return FetchResult(
        url=entry.url,
        content=content,
        content_type=entry.content_type,
        retrieved_at=entry.retrieved_at,
        data_mode=mode,
    )


def record_fixture(store_root: Path, result: FetchResult) -> None:
    """Persist a live result as a fixture and register it in ``MANIFEST.json``."""
    if result.data_mode is not DataMode.LIVE:
        raise ValueError(f"refusing to record non-live data ({result.data_mode}) as a fixture")
    DiskStore(store_root / "http").write(
        result.url, result.content, result.content_type, result.retrieved_at
    )
    manifest_path = store_root / "MANIFEST.json"
    manifest: dict[str, dict[str, str]] = (
        json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else {}
    )
    manifest[cache_key(result.url)] = {
        "url": result.url,
        "recorded_at": result.retrieved_at.isoformat(),
    }
    manifest_path.write_text(
        json.dumps(dict(sorted(manifest.items())), indent=2, ensure_ascii=False) + "\n",
        "utf-8",
        newline="\n",
    )
