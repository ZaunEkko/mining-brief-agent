"""Ask mode: LLM-driven tool use over in-memory MCP servers, and provider sessions."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from anthropic.types.beta import BetaMessage
from pydantic import SecretStr

from mining_brief.agent.ask import ask
from mining_brief.agent.events import Event
from mining_brief.agent.llm import (
    AnthropicLLM,
    AssistantTurn,
    OpenAICompatibleLLM,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from mining_brief.agent.toolbox import ServerName, Target, Toolbox, split_tool_name
from mining_brief.common.config import Settings
from mining_brief.servers.news.server import build_server as news_server
from mining_brief.servers.pdf.server import build_server as pdf_server
from mining_brief.servers.price.server import build_server as price_server
from mining_brief.servers.price.sources import SINA_LC_URL

pytestmark = pytest.mark.anyio


def _targets(settings: Settings) -> dict[ServerName, Target]:
    return {
        "news": news_server(settings),
        "pdf": pdf_server(settings),
        "price": price_server(settings),
    }


class ScriptedSession:
    """Replays a fixed list of assistant turns and records what the agent sent back."""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.submitted: list[list[ToolResult]] = []

    async def start(self, user: str) -> AssistantTurn:
        return self.turns.pop(0)

    async def submit(self, results: list[ToolResult]) -> AssistantTurn:
        self.submitted.append(results)
        return self.turns.pop(0)


class ScriptedLLM:
    name = "scripted:test"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.session = ScriptedSession(turns)
        self.tools: list[ToolSpec] = []

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ScriptedSession:
        self.tools = tools
        return self.session


def test_split_tool_name() -> None:
    assert split_tool_name("price__get_trend") == ("price", "get_trend")
    assert split_tool_name("bogus__x") is None
    assert split_tool_name("get_trend") is None


async def test_ask_runs_tools_reports_errors_and_flags_unverified_urls(
    offline_settings: Settings,
) -> None:
    llm = ScriptedLLM(
        [
            AssistantTurn(
                text="先查价格。",
                calls=[
                    ToolCall("a", "price__get_trend", {"commodity": "lithium", "days": 30}),
                    ToolCall("b", "nonexistent__tool", {}),
                ],
            ),
            AssistantTurn(
                text=f"碳酸锂下跌。[来源]({SINA_LC_URL}) [假的](https://fake.example/x)",
                calls=[],
            ),
        ]
    )
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    async with Toolbox(_targets(offline_settings), 30, sink) as tb:
        answer = await ask("锂价怎么样", tb, llm, sink)

    assert {t.name for t in llm.tools} >= {
        "news__search",
        "pdf__extract_resources",
        "price__get_trend",
    }
    results = {r.call_id: r for r in llm.session.submitted[0]}
    assert not results["a"].is_error
    assert json.loads(results["a"].content)["unit"] == "CNY/t"
    assert results["b"].is_error
    assert "unknown tool" in results["b"].content
    assert answer.unverified_urls == ["https://fake.example/x"]
    assert "未经验证" in answer.markdown
    assert answer.steps == 1
    types = [e.type for e in events]
    assert types[0] == "llm"
    assert "tool_call" in types
    assert "tool_result" in types
    assert "text" in types


async def test_ask_enforces_call_budget(offline_settings: Settings) -> None:
    call = ToolCall("x", "price__get_price", {"commodity": "copper"})
    llm = ScriptedLLM(
        [
            AssistantTurn(None, [call, ToolCall("y", "price__get_price", {"commodity": "zinc"})]),
            AssistantTurn(None, [ToolCall("z", "price__get_price", {"commodity": "tin"})]),
            AssistantTurn("不该到这里", []),
        ]
    )

    async with Toolbox(_targets(offline_settings), 30) as tb:
        answer = await ask("q", tb, llm, max_calls=1)

    first = {r.call_id: r for r in llm.session.submitted[0]}
    assert not first["x"].is_error
    assert first["y"].content == "call budget exhausted"
    assert answer.stopped_early
    assert "上限" in answer.markdown
    assert len(answer.calls) == 1


# --- provider sessions ----------------------------------------------------------


class _RecordingMessages:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> BetaMessage:
        # Snapshot: the session appends to the same list after this call returns.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return BetaMessage.model_validate(self.replies.pop(0))


def _reply(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


async def test_anthropic_tool_session_round_trip(settings: Settings) -> None:
    messages = _RecordingMessages(
        [
            _reply(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "price__get_trend",
                        "input": {"commodity": "cu"},
                    },
                ],
                "tool_use",
            ),
            _reply([{"type": "text", "text": "done"}], "end_turn"),
        ]
    )
    llm = AnthropicLLM(settings, SimpleNamespace(beta=SimpleNamespace(messages=messages)))  # type: ignore[arg-type]
    session = llm.tool_session("sys", [ToolSpec("price__get_trend", "d", {"type": "object"})])

    first = await session.start("q")
    final = await session.submit([ToolResult("tu1", '{"ok": 1}')])

    assert first.calls == [ToolCall("tu1", "price__get_trend", {"commodity": "cu"})]
    assert final.text == "done"
    second = messages.calls[1]
    assert second["tools"][0]["input_schema"] == {"type": "object"}
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert [b.type for b in assistant["content"]] == ["thinking", "tool_use"], "content unchanged"
    assert second["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu1",
        "content": '{"ok": 1}',
        "is_error": False,
    }


@respx.mock
async def test_openai_tool_session_round_trip(settings: Settings) -> None:
    replies = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "news__search",
                                    "arguments": '{"query": "PLS"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"role": "assistant", "content": "答案"}}]},
    ]
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        side_effect=[httpx.Response(200, json=r) for r in replies]
    )
    cfg = settings.model_copy(
        update={
            "openai_base_url": "https://llm.example/v1",
            "openai_api_key": SecretStr("k"),
            "openai_model": "m",
        }
    )
    session = OpenAICompatibleLLM(cfg).tool_session(
        "sys", [ToolSpec("news__search", "d", {"type": "object"})]
    )

    first = await session.start("q")
    final = await session.submit([ToolResult("c1", "boom", is_error=True)])

    assert first.calls == [ToolCall("c1", "news__search", {"query": "PLS"})]
    assert final.text == "答案"
    body = json.loads(route.calls[1].request.content)
    assert body["tools"][0]["function"]["name"] == "news__search"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: boom"}
