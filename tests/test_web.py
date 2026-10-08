"""Web API: SSE streams and tool explorer over in-memory MCP servers."""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest

from mining_brief.agent.llm import AssistantTurn, ToolCall
from mining_brief.agent.toolbox import ServerName, Target
from mining_brief.common.config import Settings
from mining_brief.servers.news.server import build_server as news_server
from mining_brief.servers.pdf.server import build_server as pdf_server
from mining_brief.servers.price.server import build_server as price_server
from mining_brief.web.app import create_app
from tests.test_ask import ScriptedLLM

pytestmark = pytest.mark.anyio


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
async def make_client(offline_settings: Settings, tmp_path: Path) -> AsyncIterator[Any]:
    clients: list[httpx.AsyncClient] = []
    stack = AsyncExitStack()

    async def factory(llm: Any = None) -> httpx.AsyncClient:
        def targets() -> dict[ServerName, Target]:
            return {
                "news": news_server(offline_settings),
                "pdf": pdf_server(offline_settings),
                "price": price_server(offline_settings),
            }

        app = create_app(offline_settings, targets_factory=targets, llm=llm, dist=tmp_path / "none")
        await stack.enter_async_context(app.router.lifespan_context(app))  # opens shared MCP
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        clients.append(client)
        return client

    yield factory
    for c in clients:
        await c.aclose()
    await stack.aclose()


async def test_meta_reports_llm_and_examples(make_client: Any) -> None:
    response = await (await make_client()).get("/api/meta")

    data = response.json()
    assert data["llm"] is None
    assert data["offline"] is True
    assert data["examples"]["brief"]


async def test_tools_lists_three_servers(make_client: Any) -> None:
    data = (await (await make_client()).get("/api/tools")).json()

    assert [s["server"] for s in data] == ["news", "pdf", "price"]
    assert all(s["connected"] for s in data)
    names = {t["name"] for s in data for t in s["tools"]}
    assert names == {"search", "fetch_article", "extract_resources", "get_price", "get_trend"}


async def test_call_tool_ok_and_error(make_client: Any) -> None:
    client = await make_client()
    ok = (
        await client.post("/api/tools/price/get_price", json={"arguments": {"commodity": "cu"}})
    ).json()
    bad = (
        await client.post("/api/tools/price/get_price", json={"arguments": {"commodity": "x"}})
    ).json()
    missing = await client.post("/api/tools/nope/x", json={})

    assert ok["ok"] is True
    assert ok["result"]["unit"] == "USD/t"
    assert bad["ok"] is False
    assert "unknown commodity" in bad["error"]
    assert missing.status_code == 404


async def test_brief_streams_progress_then_markdown(make_client: Any) -> None:
    response = await (await make_client()).post("/api/brief", json={"use_llm": False})

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    types = [t for t, _ in events]
    assert types[0] == "plan"
    assert types[-1] == "done"
    assert types.count("tool_call") == types.count("tool_result") >= 4
    assert "## 二、储量数据" in events[-1][1]["markdown"]


async def test_ask_requires_llm(make_client: Any) -> None:
    response = await (await make_client()).post("/api/ask", json={"question": "锂价？"})

    assert response.status_code == 409


async def test_ask_streams_tool_calls(make_client: Any) -> None:
    llm = ScriptedLLM(
        [
            AssistantTurn(None, [ToolCall("a", "price__get_price", {"commodity": "lithium"})]),
            AssistantTurn("碳酸锂最新结算价见上。", []),
        ]
    )

    response = await (await make_client(llm)).post("/api/ask", json={"question": "锂价？"})

    events = _parse_sse(response.text)
    assert [t for t, _ in events][:4] == ["llm", "llm_done", "tool_call", "tool_result"]
    assert events[1][1]["calls"] == 1
    done = events[-1][1]
    assert done["calls"] == [{"name": "price__get_price", "arguments": {"commodity": "lithium"}}]
    assert "碳酸锂" in done["markdown"]
