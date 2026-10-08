"""mineral-pdf-mcp: Mineral Resource extraction from NI 43-101 / JORC report PDFs."""

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
from mining_brief.servers.pdf.service import (
    DEFAULT_SCAN_PAGES,
    PdfError,
    PdfService,
    ResourceExtraction,
)

DEFAULT_PORT = 8002


def build_server(settings: Settings | None = None) -> MCPServer:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[PdfService]:
        http = CachedHttp(cfg)
        try:
            yield PdfService(http)
        finally:
            await http.aclose()

    mcp = MCPServer(
        "mineral-pdf-mcp",
        instructions=(
            "Extracts Measured/Indicated/Inferred Mineral Resources (tonnage, grade, contained "
            "metal) from NI 43-101 or JORC report PDFs. Every result carries consistency "
            "checks; when abstain is true, do not present the numbers as fact."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
    )
    async def extract_resources(
        pdf_url: Annotated[str, Field(description="Public http(s) URL of the report PDF")],
        ctx: Context[PdfService, Any],
        max_pages: Annotated[
            int, Field(description="Pages to scan from the start", ge=1, le=400)
        ] = DEFAULT_SCAN_PAGES,
    ) -> ResourceExtraction:
        """Extract the Indicated and Inferred Mineral Resource table from a report PDF."""
        try:
            return await ctx.request_context.lifespan_context.extract_resources(pdf_url, max_pages)
        except PdfError as exc:
            raise ToolError(str(exc)) from exc

    return mcp


def main(argv: list[str] | None = None) -> None:
    run_server(build_server(), default_port=DEFAULT_PORT, argv=argv)


if __name__ == "__main__":
    main()
