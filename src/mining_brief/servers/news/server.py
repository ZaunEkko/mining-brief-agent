"""mining-news-mcp: mining news search and full-text article fetch."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mining_brief.common.config import Settings, get_settings
from mining_brief.common.http import CachedHttp
from mining_brief.common.serve import run_server
from mining_brief.servers.news.service import (
    MAX_DAYS,
    MAX_RESULTS,
    Article,
    NewsError,
    NewsSearchResult,
    NewsService,
)

DEFAULT_PORT = 8001
_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


def build_server(settings: Settings | None = None) -> MCPServer:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[NewsService]:
        http = CachedHttp(cfg)
        try:
            yield NewsService(http)
        finally:
            await http.aclose()

    mcp = MCPServer(
        "mining-news-mcp",
        instructions=(
            "Mining news from mining.com RSS (search, commodity and front-page feeds) and "
            "Google News. Use search first, then fetch_article on items whose "
            "full_text_available is true."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(annotations=_READ_ONLY)
    async def search(
        query: Annotated[str, Field(description="Keywords, e.g. 'Pilbara lithium'")],
        ctx: Context[NewsService, Any],
        days: Annotated[int, Field(description="Look-back window in days", ge=1, le=MAX_DAYS)] = 7,
        limit: Annotated[int, Field(description="Maximum items", ge=1, le=MAX_RESULTS)] = 20,
    ) -> NewsSearchResult:
        """Search recent mining news; results are deduplicated and ranked by relevance."""
        try:
            return await ctx.request_context.lifespan_context.search(query, days, limit)
        except NewsError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=_READ_ONLY)
    async def fetch_article(
        url: Annotated[str, Field(description="Article URL from a search result")],
        ctx: Context[NewsService, Any],
        max_chars: Annotated[int, Field(description="Truncate text", ge=500, le=50_000)] = 8000,
    ) -> Article:
        """Download an article and return its main text and metadata."""
        try:
            return await ctx.request_context.lifespan_context.fetch_article(url, max_chars)
        except NewsError as exc:
            raise ToolError(str(exc)) from exc

    return mcp


def main(argv: list[str] | None = None) -> None:
    run_server(build_server(), default_port=DEFAULT_PORT, argv=argv)


if __name__ == "__main__":
    main()
