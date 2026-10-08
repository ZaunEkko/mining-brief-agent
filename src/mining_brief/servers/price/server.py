"""lme-price-mcp: daily base-metal (LME) and lithium carbonate (GFEX) prices."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mining_brief.common.config import Settings, get_settings
from mining_brief.common.http import CachedHttp, FetchError
from mining_brief.common.serve import run_server
from mining_brief.servers.price.service import (
    PriceLookupError,
    PriceQuote,
    PriceService,
    PriceTrend,
)
from mining_brief.servers.price.sources import ParseError

DEFAULT_PORT = 8003
_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
CommodityArg = Annotated[
    str,
    Field(
        description=(
            "copper | zinc | nickel | aluminium | lead | tin | lithium_carbonate "
            "(aliases such as 'cu', 'lithium', '铜', '碳酸锂' are accepted)"
        )
    ),
]


def build_server(settings: Settings | None = None) -> MCPServer:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[PriceService]:
        http = CachedHttp(cfg)
        try:
            yield PriceService(http)
        finally:
            await http.aclose()

    mcp = MCPServer(
        "lme-price-mcp",
        instructions=(
            "Daily metal prices. LME base metals come from westmetall's public LME table "
            "(USD/t cash settlement); lithium carbonate comes from GFEX via Sina Finance (CNY/t)."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_price(
        commodity: CommodityArg,
        ctx: Context[PriceService, Any],
        date: Annotated[
            str | None,
            Field(description="ISO date YYYY-MM-DD; omit for the latest trading day"),
        ] = None,
    ) -> PriceQuote:
        """Price of a commodity on a date (or the closest earlier trading day)."""
        service = ctx.request_context.lifespan_context
        try:
            on = _parse_date(date)
            return await service.get_price(commodity, on)
        except (PriceLookupError, FetchError, ParseError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=_READ_ONLY)
    async def get_trend(
        commodity: CommodityArg,
        ctx: Context[PriceService, Any],
        days: Annotated[int, Field(description="Calendar-day window ending today", ge=2)] = 30,
    ) -> PriceTrend:
        """Price series over the last N calendar days with change, high, low and direction."""
        service = ctx.request_context.lifespan_context
        try:
            return await service.get_trend(commodity, days)
        except (PriceLookupError, FetchError, ParseError) as exc:
            raise ToolError(str(exc)) from exc

    return mcp


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PriceLookupError(f"date must be YYYY-MM-DD, got {value!r}") from exc


def main(argv: list[str] | None = None) -> None:
    run_server(build_server(), default_port=DEFAULT_PORT, argv=argv)


if __name__ == "__main__":
    main()
