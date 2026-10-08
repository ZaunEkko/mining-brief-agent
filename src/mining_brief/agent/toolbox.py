"""MCP client side: one connection per server, failures isolated per server."""

import logging
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Protocol, Self

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, Tool

from mining_brief.agent.events import EventSink, emit, summarize
from mining_brief.agent.llm import ToolSpec
from mining_brief.common.config import Settings

logger = logging.getLogger(__name__)

ServerName = Literal["news", "pdf", "price"]
Target = str | StdioServerParameters | MCPServer

SERVER_MODULES: dict[ServerName, str] = {
    "news": "mining_brief.servers.news.server",
    "pdf": "mining_brief.servers.pdf.server",
    "price": "mining_brief.servers.price.server",
}
SERVER_LABELS: dict[ServerName, str] = {
    "news": "mining-news-mcp",
    "pdf": "mineral-pdf-mcp",
    "price": "lme-price-mcp",
}
_SEP = "__"  # namespaced tool names for LLMs: "news__search"


@dataclass(frozen=True, slots=True)
class ToolFailure:
    server: ServerName
    tool: str
    message: str

    def __str__(self) -> str:
        return f"{self.server}.{self.tool}: {self.message}"


def targets_from_settings(settings: Settings) -> dict[ServerName, Target]:
    """HTTP URL when configured (docker-compose), otherwise a stdio subprocess."""
    urls: dict[ServerName, str | None] = {
        "news": settings.news_url,
        "pdf": settings.pdf_url,
        "price": settings.price_url,
    }
    env = settings.server_env()
    return {
        name: url
        or StdioServerParameters(command=sys.executable, args=["-m", SERVER_MODULES[name]], env=env)
        for name, url in urls.items()
    }


def split_tool_name(name: str) -> tuple[ServerName, str] | None:
    server, sep, tool = name.partition(_SEP)
    if not sep or server not in SERVER_MODULES:
        return None
    return server, tool


class Toolbox:
    def __init__(
        self,
        targets: dict[ServerName, Target],
        timeout_s: float,
        on_event: EventSink | None = None,
    ) -> None:
        self._targets = targets
        self._timeout_s = timeout_s
        self._on_event = on_event
        self._clients: dict[ServerName, Client] = {}
        self._connect_errors: dict[ServerName, str] = {}
        self._list_errors: dict[ServerName, str] = {}
        self._stack = AsyncExitStack()
        self._seq = 0

    async def __aenter__(self) -> Self:
        await self._stack.__aenter__()
        for name, target in self._targets.items():
            try:
                client = Client(target, read_timeout_seconds=self._timeout_s)
                self._clients[name] = await self._stack.enter_async_context(client)
            except Exception as exc:  # one unreachable server must not sink the brief
                logger.warning("cannot connect to %s server: %r", name, exc)
                self._connect_errors[name] = f"server unreachable ({type(exc).__name__}: {exc})"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stack.__aexit__(exc_type, exc, tb)

    @property
    def connected(self) -> dict[ServerName, bool]:
        return {name: name in self._clients for name in self._targets}

    async def tool_specs(self) -> list[ToolSpec]:
        """Every tool of every connected server, namespaced as ``<server>__<tool>``."""
        specs: list[ToolSpec] = []
        for name, tools in (await self._list_all()).items():
            for tool in tools:
                specs.append(
                    ToolSpec(
                        name=f"{name}{_SEP}{tool.name}",
                        description=f"[{SERVER_LABELS[name]}] {tool.description or ''}".strip(),
                        input_schema=dict(tool.input_schema),
                    )
                )
        return specs

    async def describe(self) -> list[dict[str, Any]]:
        """Server/tool catalogue for UIs, including output schemas."""
        listed = await self._list_all()
        out: list[dict[str, Any]] = []
        for name in self._targets:
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "output_schema": t.output_schema,
                }
                for t in listed.get(name, [])
            ]
            out.append(
                {
                    "server": name,
                    "label": SERVER_LABELS[name],
                    "connected": name in listed,
                    "error": self._list_errors.get(name) or self._connect_errors.get(name),
                    "tools": tools,
                }
            )
        return out

    async def _list_all(self) -> dict[ServerName, list[Tool]]:
        """``list_tools`` on every connected server; one failure only hides that server."""
        listed: dict[ServerName, list[Tool]] = {}
        self._list_errors = {}
        for name, client in self._clients.items():
            try:
                listed[name] = list((await client.list_tools()).tools)
            except Exception as exc:  # a dropped connection must not hide healthy servers
                logger.warning("list_tools failed on %s: %r", name, exc)
                self._list_errors[name] = f"list_tools failed ({type(exc).__name__}: {exc})"
        return listed

    def bind(self, on_event: EventSink | None) -> "BoundToolbox":
        """A view that shares these connections but reports events to ``on_event``
        (one long-lived Toolbox serving many concurrent web requests)."""
        return BoundToolbox(self, on_event)

    async def call(
        self,
        server: ServerName,
        tool: str,
        arguments: dict[str, Any],
        on_event: EventSink | None = None,
    ) -> dict[str, Any] | ToolFailure:
        sink = on_event or self._on_event
        self._seq += 1
        call_id = f"c{self._seq}"
        await emit(sink, "tool_call", id=call_id, server=server, tool=tool, arguments=arguments)
        started = time.perf_counter()
        result = await self._call(server, tool, arguments)
        ms = round((time.perf_counter() - started) * 1000)
        if isinstance(result, ToolFailure):
            await emit(
                sink, "tool_result", id=call_id, server=server, tool=tool,
                ok=False, ms=ms, summary=result.message,
            )  # fmt: skip
        else:
            await emit(
                sink, "tool_result", id=call_id, server=server, tool=tool,
                ok=True, ms=ms, summary=summarize(tool, result),
            )  # fmt: skip
        return result

    async def _call(
        self, server: ServerName, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | ToolFailure:
        client = self._clients.get(server)
        if client is None:
            return ToolFailure(server, tool, self._connect_errors.get(server, "not connected"))
        try:
            result = await client.call_tool(tool, arguments)
        except Exception as exc:  # transport errors, timeouts
            logger.warning("%s.%s failed: %r", server, tool, exc)
            return ToolFailure(server, tool, f"{type(exc).__name__}: {exc}")
        if result.is_error or result.structured_content is None:
            text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
            return ToolFailure(server, tool, text or "tool returned no structured content")
        return dict(result.structured_content)


class BoundToolbox:
    """Toolbox calls with a per-request event sink."""

    def __init__(self, toolbox: Toolbox, on_event: EventSink | None) -> None:
        self._toolbox = toolbox
        self._on_event = on_event

    async def tool_specs(self) -> list[ToolSpec]:
        return await self._toolbox.tool_specs()

    async def describe(self) -> list[dict[str, Any]]:
        return await self._toolbox.describe()

    async def call(
        self, server: ServerName, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | ToolFailure:
        return await self._toolbox.call(server, tool, arguments, self._on_event)


class Tools(Protocol):
    """What the agent needs from a toolbox: a plain Toolbox or a bound view."""

    async def tool_specs(self) -> list[ToolSpec]: ...

    async def call(
        self, server: ServerName, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | ToolFailure: ...
