"""Progress events shared by the CLI and the web UI.

Both modes report what they do as a stream of small JSON-able events, so the UI can
show each MCP tool call as it happens instead of a spinner.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EventType = Literal[
    "plan", "tool_call", "tool_result", "llm", "llm_done", "text", "warning", "done", "error"
]


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


EventSink = Callable[[Event], Awaitable[None]]


@asynccontextmanager
async def llm_span(
    sink: EventSink | None, span_id: str, model: str, phase: str
) -> AsyncIterator[dict[str, Any]]:
    """Report one LLM round as ``llm`` ... ``llm_done`` so UIs can show where time goes.

    The body may put ``calls`` (number of tool calls requested) into the yielded dict.
    """
    await emit(sink, "llm", id=span_id, model=model, phase=phase)
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    ok = False
    try:
        yield extra
        ok = True
    finally:
        ms = round((time.perf_counter() - started) * 1000)
        await emit(sink, "llm_done", id=span_id, ms=ms, ok=ok, **extra)


async def emit(sink: EventSink | None, type_: EventType, **data: Any) -> None:
    if sink is not None:
        await sink(Event(type_, data))


def summarize(tool: str, result: dict[str, Any]) -> str:
    """One-line human summary of a tool result for progress displays."""
    if tool == "search":
        return f"{len(result.get('items', []))} 条新闻，{len(result.get('sources', []))} 个源"
    if tool == "fetch_article":
        return f"正文 {result.get('word_count', 0)} 词"
    if tool == "extract_resources":
        table = result.get("table", {})
        return f"第 {table.get('page')} 页，置信度 {table.get('confidence')}" + (
            "（abstain）" if result.get("abstain") else ""
        )
    if tool == "get_price":
        return f"{result.get('price')} {result.get('unit')}（{result.get('trading_date')}）"
    if tool == "get_trend":
        return f"{result.get('change_pct')}%（{result.get('window_days')} 天）"
    return "ok"
