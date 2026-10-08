"""Agent: planning, composition guards, end-to-end over in-memory MCP servers."""

import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from anthropic.types.beta import BetaMessage
from mcp import StdioServerParameters
from pydantic import SecretStr

from mining_brief.agent.compose import RISK_PATTERNS, SourceRegistry, select_news
from mining_brief.agent.intent import plan_brief
from mining_brief.agent.llm import AnthropicLLM, LLMError, OpenAICompatibleLLM, build_llm
from mining_brief.agent.orchestrator import generate_brief
from mining_brief.agent.render import sparkline
from mining_brief.agent.toolbox import ServerName, Target, targets_from_settings
from mining_brief.common.config import Settings
from mining_brief.servers.news.server import build_server as news_server
from mining_brief.servers.pdf.server import build_server as pdf_server
from mining_brief.servers.price.server import build_server as price_server

pytestmark = pytest.mark.anyio

DEMO = "给我生成一份关于 Pilbara 锂矿的今日简报"
NOW = datetime.fromisoformat("2026-10-08T18:00:00+08:00")


def _targets(settings: Settings) -> dict[ServerName, Target]:
    return {
        "news": news_server(settings),
        "pdf": pdf_server(settings),
        "price": price_server(settings),
    }


def _citations(markdown: str) -> tuple[set[int], set[int]]:
    body, _, refs = markdown.partition("## 引用来源")
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    listed = {int(n) for n in re.findall(r"^(\d+)\. \[", refs, re.M)}
    return cited, listed


# --- planning -----------------------------------------------------------------


def test_plan_demo_query_uses_catalog() -> None:
    plan = plan_brief(DEMO)

    assert plan.project is not None
    assert plan.project.name == "Pilgangoora"
    assert plan.commodity == "lithium_carbonate"
    assert plan.report is not None
    assert plan.news_days == 7


def test_plan_parses_window_and_free_text_commodity() -> None:
    plan = plan_brief("近 14 天 Escondida 铜矿简报")

    assert plan.project is None
    assert plan.commodity == "copper"
    assert plan.news_days == 14
    assert plan.news_query == "Escondida"


def test_alias_matching_is_whole_word() -> None:
    assert plan_brief("give me samples of nickel news").project is None


# --- composition guards -------------------------------------------------------


def test_sparkline_shape() -> None:
    assert sparkline([1, 2, 3, 4]) == "▁▃▆█"
    assert len(sparkline(list(range(100)), width=24)) == 24
    assert set(sparkline([5, 5, 5])) == {"▄"}


@pytest.mark.parametrize(
    ("text", "label"),
    [("The miner halted operations", "停产/暂停"), ("new royalty regime", "税费/特许权")],
)
def test_risk_patterns_match_whole_words(text: str, label: str) -> None:
    assert re.search(RISK_PATTERNS[label], text)


def test_risk_patterns_ignore_substrings() -> None:
    text = "the bank's urban strategy, shutterstock image, taxonomy of courtesy"
    assert not any(re.search(p, text) for p in RISK_PATTERNS.values())


def test_select_news_splits_project_and_market() -> None:
    items = [
        {"title": "Pilbara iron ore assays", "summary": ""},
        {"title": "PLS lifts Pilgangoora output", "summary": ""},
        {"title": "Lithium prices slide", "summary": ""},
    ]

    groups = [(i["title"], g) for i, g in select_news(items, ("pilgangoora", "pls"))]

    assert groups == [
        ("PLS lifts Pilgangoora output", "project"),
        ("Pilbara iron ore assays", "market"),
        ("Lithium prices slide", "market"),
    ]


def test_source_registry_dedupes_and_validates() -> None:
    reg = SourceRegistry()
    a = reg.add("https://a", "A")
    assert reg.add("https://a", "A again") == a
    assert reg.valid([a, 99]) == [a]


# --- end to end ---------------------------------------------------------------


async def test_offline_brief_has_every_section(offline_settings: Settings) -> None:
    brief = await generate_brief(DEMO, _targets(offline_settings), now=NOW)
    md = brief.markdown

    for heading in (
        "## 一、新闻摘要",
        "**项目动态**",
        "## 二、储量数据",
        "## 三、价格走势",
        "## 四、风险提示",
    ):
        assert heading in md
    assert "| 控制 Indicated | 356 | 1.29 | 4.6 |" in md
    assert "| 推断 Inferred | 70 | 1.25 | 0.9 |" in md
    assert "CNY/t" in md
    assert "合成示例数据（离线）" in md
    cited, listed = _citations(md)
    assert cited
    assert cited <= listed, "every citation must resolve to a listed source"


async def test_missing_server_degrades_only_its_section(offline_settings: Settings) -> None:
    targets = _targets(offline_settings)
    targets["pdf"] = "http://127.0.0.1:9/mcp"  # nothing listens here

    brief = await generate_brief(DEMO, targets, timeout_s=5, now=NOW)

    assert "## 二、储量数据\n\n> 数据暂不可用" in brief.markdown
    assert "| 碳酸锂" in brief.markdown
    assert brief.content.news


class FakeLLM:
    name = "fake:test"

    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def test_llm_output_is_guarded(offline_settings: Settings) -> None:
    llm = FakeLLM(
        {
            "overview": "锂价走弱，PLS 维持扩产节奏。",
            "news": [{"id": 1, "summary": "中文摘要一"}],
            "risks": [
                {"text": "有据可查的风险", "sources": [1]},
                {"text": "编造来源的风险", "sources": [99]},
                {"text": "没有来源的风险", "sources": []},
            ],
        }
    )

    brief = await generate_brief(DEMO, _targets(offline_settings), llm=llm, now=NOW)
    md = brief.markdown

    assert "**今日要点**：锂价走弱" in md
    assert "中文摘要一" in md
    assert "有据可查的风险 [1]" in md
    assert "编造来源的风险" not in md
    assert "没有来源的风险" not in md
    assert "资源量报告发布已" in md, "rule-based data-quality risks are kept"
    assert "fake:test" in md
    material = json.loads(llm.calls[0])
    assert {"subject", "news", "facts"} <= material.keys()


async def test_llm_failure_falls_back_to_deterministic(offline_settings: Settings) -> None:
    brief = await generate_brief(
        DEMO, _targets(offline_settings), llm=FakeLLM(LLMError("boom")), now=NOW
    )

    assert "LLM 调用失败，已降级：boom" in brief.markdown
    assert "deterministic (LLM failed: boom)" in brief.markdown


# --- wiring and LLM clients ---------------------------------------------------


def test_stdio_targets_forward_settings(offline_settings: Settings) -> None:
    news = targets_from_settings(offline_settings)["news"]

    assert isinstance(news, StdioServerParameters)
    assert news.args == ["-m", "mining_brief.servers.news.server"]
    assert news.env is not None
    assert news.env["MB_OFFLINE"] == "1"


def test_http_targets_when_urls_configured(settings: Settings) -> None:
    cfg = settings.model_copy(update={"news_url": "http://news:8001/mcp"})
    assert targets_from_settings(cfg)["news"] == "http://news:8001/mcp"


def test_build_llm_selection(settings: Settings) -> None:
    assert build_llm(settings) is None  # provider "none" in tests
    auto = settings.model_copy(update={"llm_provider": "auto"})
    assert build_llm(auto) is None
    keyed = auto.model_copy(update={"anthropic_api_key": SecretStr("sk-test")})
    assert isinstance(build_llm(keyed), AnthropicLLM)


class _StubMessages:
    """Stands in for ``client.beta.messages`` so no request leaves the process."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> BetaMessage:
        self.kwargs = kwargs
        return BetaMessage.model_validate(self.reply)


def _stub_client(messages: _StubMessages) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def _message(stop_reason: str, text: str | None) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text is not None else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


async def test_anthropic_request_shape(settings: Settings) -> None:
    stub = _StubMessages(_message("end_turn", '{"overview": "ok", "news": [], "risks": []}'))

    data = await AnthropicLLM(settings, _stub_client(stub)).complete_json(
        "sys", "u", {"type": "object"}
    )

    assert data["overview"] == "ok"
    assert stub.kwargs["model"] == "claude-opus-5-5"
    assert stub.kwargs["system"] == "sys"
    assert stub.kwargs["output_config"]["format"] == {
        "type": "json_schema",
        "schema": {"type": "object"},
    }
    assert stub.kwargs["output_config"]["effort"] == "medium"
    assert stub.kwargs["fallbacks"] == "default"
    assert stub.kwargs["betas"] == ["server-side-fallback-2026-07-01"]


async def test_anthropic_fallback_can_be_disabled(settings: Settings) -> None:
    stub = _StubMessages(_message("end_turn", "{}"))
    cfg = settings.model_copy(update={"anthropic_server_fallback": False})

    await AnthropicLLM(cfg, _stub_client(stub)).complete_json("sys", "u", {"type": "object"})

    assert "fallbacks" not in stub.kwargs
    assert "betas" not in stub.kwargs


@pytest.mark.parametrize(
    ("stop_reason", "text", "message"),
    [
        ("refusal", None, "declined"),
        ("max_tokens", "{", "truncated"),
        ("end_turn", "not json", "invalid JSON"),
    ],
)
async def test_anthropic_bad_outputs_raise(
    settings: Settings, stop_reason: str, text: str | None, message: str
) -> None:
    llm = AnthropicLLM(settings, _stub_client(_StubMessages(_message(stop_reason, text))))

    with pytest.raises(LLMError, match=message):
        await llm.complete_json("sys", "u", {"type": "object"})


@respx.mock
async def test_openai_compatible_request_shape(settings: Settings) -> None:
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"overview": "好", "news": [], "risks": []}'}}]
            },
        )
    )
    cfg = settings.model_copy(
        update={
            "openai_base_url": "https://llm.example/v1",
            "openai_api_key": SecretStr("k"),
            "openai_model": "deepseek-chat",
        }
    )

    data = await OpenAICompatibleLLM(cfg).complete_json("sys", "user", {"type": "object"})

    assert data["overview"] == "好"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


def _openai_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "openai_base_url": "https://llm.example/v1",
            "openai_api_key": SecretStr("k"),
            "openai_model": "m",
        }
    )


@respx.mock
async def test_openai_html_response_points_at_base_url(settings: Settings) -> None:
    respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, text="<!doctype html>", headers={"content-type": "text/html"}
        )
    )

    with pytest.raises(LLMError, match="trailing /v1"):
        await OpenAICompatibleLLM(_openai_settings(settings)).complete_json("s", "u", {})


@respx.mock
async def test_openai_retries_transient_errors(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mining_brief.agent.llm.OPENAI_BACKOFF_S", 0)
    ok = {"choices": [{"message": {"content": '{"overview": "x"}'}}]}
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        side_effect=[httpx.Response(503, json={"error": "busy"}), httpx.Response(200, json=ok)]
    )

    data = await OpenAICompatibleLLM(_openai_settings(settings)).complete_json("s", "u", {})

    assert data == {"overview": "x"}
    assert route.call_count == 2


@respx.mock
async def test_openai_client_errors_are_not_retried(settings: Settings) -> None:
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )

    with pytest.raises(LLMError, match="HTTP 401"):
        await OpenAICompatibleLLM(_openai_settings(settings)).complete_json("s", "u", {})
    assert route.call_count == 1


def test_merge_risks_drops_duplicated_price_and_news_rules() -> None:
    from mining_brief.agent.compose import Risk, merge_risks

    llm = [Risk("锂价走弱", [10])]
    rules = [
        Risk("新闻中出现「司法/诉讼」相关信号", [7], "news"),
        Risk("碳酸锂 近 30 天下跌 14.4%", [10], "price"),
        Risk("LME 铜 区间振幅 16%", [11], "price"),
        Risk("资源量报告发布已 484 天", [], "data"),
    ]

    merged = [r.text for r in merge_risks(llm, rules)]

    assert merged == ["锂价走弱", "LME 铜 区间振幅 16%", "资源量报告发布已 484 天"]
