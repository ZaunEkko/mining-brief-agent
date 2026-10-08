"""mining-news-mcp: feed parsing, ranking and MCP contract tests over recorded fixtures."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client

from mining_brief.common.config import Settings
from mining_brief.common.http import DiskStore
from mining_brief.common.urls import UnsafeUrlError, ensure_public_url
from mining_brief.servers.news.server import build_server
from mining_brief.servers.news.service import rank
from mining_brief.servers.news.sources import (
    MINING_FRONT,
    FeedEntry,
    FeedError,
    parse_feed,
    search_feed_urls,
    terms,
)
from tests.conftest import REPO_FIXTURES

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _entry(
    title: str, *, days_ago: int = 1, url: str | None = None, full: bool = True
) -> FeedEntry:
    return FeedEntry(
        title=title,
        url=url or f"https://www.mining.com/{title.lower().replace(' ', '-')}/",
        published_at=NOW - timedelta(days=days_ago),
        summary=f"{title} summary",
        content="",
        publisher="MINING.COM" if full else "Reuters",
        full_text_available=full,
        feed_url="https://example.test/feed",
    )


def test_terms_drop_stopwords_but_keep_cjk() -> None:
    assert terms("The latest Pilbara lithium news") == ["pilbara", "lithium"]
    assert terms("锂 a") == ["锂"]


def test_search_feed_urls_add_commodity_feed() -> None:
    urls = search_feed_urls("Pilbara lithium", 7)

    assert "https://www.mining.com/commodity/lithium/feed/" in urls
    assert MINING_FRONT in urls
    assert any("when:7d" in u for u in urls)


def test_front_page_fixture_parses() -> None:
    stored = DiskStore(REPO_FIXTURES / "http").read(MINING_FRONT)
    assert stored is not None

    entries = parse_feed(stored[1], MINING_FRONT)

    assert len(entries) >= 20
    assert all(e.full_text_available for e in entries)
    # Synthetic items link to example.com, so they must not be attributed to MINING.COM.
    assert {e.publisher for e in entries} == {"example.com"}
    assert all(e.published_at.tzinfo is UTC for e in entries)


def test_publisher_comes_from_the_article_host() -> None:
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>a</title><link>https://www.mining.com/x/</link>"
        b"<pubDate>Wed, 07 Oct 2026 10:00:00 +0000</pubDate></item>"
        b"<item><title>b</title><link>https://example.org/y</link>"
        b"<pubDate>Wed, 07 Oct 2026 10:00:00 +0000</pubDate></item></channel></rss>"
    )

    entries = parse_feed(feed, MINING_FRONT)

    assert [e.publisher for e in entries] == ["MINING.COM", "example.org"]


def test_invalid_feed_raises() -> None:
    with pytest.raises(FeedError):
        parse_feed(b"\x00not xml at all", "https://example.test/feed")


def test_rank_filters_window_and_requires_a_match() -> None:
    entries = [
        _entry("Pilbara lithium output rises"),
        _entry("Copper hits record", days_ago=1),
        _entry("Pilbara lithium old story", days_ago=30),
    ]

    items = rank(entries, "Pilbara lithium", days=7, now=NOW)

    assert [i.title for i in items] == ["Pilbara lithium output rises"]


def test_rank_prefers_title_hits_and_dedupes_keeping_full_text() -> None:
    entries = [
        _entry("Lithium market update"),
        _entry(
            "Pilbara expands lithium plant",
            full=False,
            url="https://news.google.com/rss/articles/x",
        ),
        _entry("Pilbara expands lithium plant"),
        _entry(
            "Lithium market update", url="https://www.mining.com/lithium-market-update/?utm=feed"
        ),
    ]

    items = rank(entries, "Pilbara lithium", days=7, now=NOW)

    assert [i.title for i in items] == ["Pilbara expands lithium plant", "Lithium market update"]
    assert items[0].full_text_available
    assert items[0].score > items[1].score


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost:8001/mcp",
        "http://127.0.0.1/",
        "http://10.0.0.5/x",
        "ftp://x.com/",
    ],
)
def test_ensure_public_url_rejects_local_targets(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_url(url)


@pytest.fixture
async def client(offline_settings: Settings) -> AsyncIterator[Client]:
    async with Client(build_server(offline_settings)) as c:
        yield c


async def test_lists_required_tools(client: Client) -> None:
    names = {t.name for t in (await client.list_tools()).tools}
    assert {"search", "fetch_article"} <= names


async def test_search_demo_query_offline(client: Client) -> None:
    result = await client.call_tool("search", {"query": "PLS lithium", "days": 7})

    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["items"], data["notes"]
    assert {s["data_mode"] for s in data["sources"]} == {"fixture"}
    assert any(i["full_text_available"] for i in data["items"])
    scores = [i["score"] for i in data["items"]]
    assert scores == sorted(scores, reverse=True)


async def test_fetch_article_offline(client: Client) -> None:
    search = await client.call_tool("search", {"query": "PLS lithium", "days": 7})
    assert search.structured_content is not None
    url = next(i["url"] for i in search.structured_content["items"] if i["full_text_available"])

    result = await client.call_tool("fetch_article", {"url": url, "max_chars": 1000})

    data = result.structured_content
    assert data is not None
    assert data["publisher"] == "Mining Brief Demo"  # synthetic site, see make_demo_fixtures
    assert len(data["text"]) <= 1000
    assert data["word_count"] > 100


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("search", {"query": "   "}, "must not be empty"),
        ("fetch_article", {"url": "http://127.0.0.1:8003/mcp"}, "non-public"),
        (
            "fetch_article",
            {"url": "https://news.google.com/rss/articles/abc"},
            "JavaScript redirects",
        ),
        ("fetch_article", {"url": "https://www.mining.com/not-recorded/"}, "offline mode"),
    ],
)
async def test_errors_are_tool_errors(
    client: Client, tool: str, args: dict[str, object], message: str
) -> None:
    result = await client.call_tool(tool, args)

    assert result.is_error
    assert message in str(result.content[0])
