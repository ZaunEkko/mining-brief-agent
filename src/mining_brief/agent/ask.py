"""Ask mode: the LLM decides which MCP tools to call.

The brief pipeline is a fixed plan; this mode is an open tool-use loop for
free-form questions. Guard rails: a step and call budget, tool errors returned to the
model as errors, numbers only from tool results (prompted), and a post-check that
every URL in the answer actually appeared in some tool result.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from mining_brief.agent.catalog import PROJECTS
from mining_brief.agent.events import EventSink, emit, llm_span
from mining_brief.agent.llm import LLMClient, ToolCall, ToolResult
from mining_brief.agent.toolbox import ToolFailure, Tools, split_tool_name

MAX_STEPS = 8
MAX_CALLS = 16
RESULT_CHARS = 12000
_URL = re.compile(r"https?://[^\s)\]>\"'，。]+")


def _catalog_hint() -> str:
    lines = []
    for p in PROJECTS:
        reports = "; ".join(f"{r.title} ({r.published}, {r.code}): {r.url}" for r in p.reports)
        lines.append(
            f"- {p.name} | {p.owner} | {p.region} | 品种参数 {p.commodity} | "
            f"新闻检索词 '{p.news_query}' | 资源量报告 {reports}"
        )
    return "\n".join(lines)


SYSTEM = f"""你是矿业研究助手，通过工具获取数据后用中文回答用户问题。

规则：
1. 涉及新闻、价格、储量的事实必须先调用工具获取，不要凭记忆回答；数字只能来自工具结果。
2. 工具可以多次、并行调用；工具报错时根据错误信息调整参数重试或换用其他工具。
3. extract_resources 返回 abstain=true 时，不得引用其中的数字，只说明校验未通过。
4. 回答使用 Markdown，在相关句子后用 [来源](URL) 标注出处，URL 必须原样来自工具结果。
5. 数据不足时直接说明缺什么，不要猜测。

已知项目（可直接使用其中的报告 URL 与检索词）：
{_catalog_hint()}
"""


@dataclass(slots=True)
class Answer:
    markdown: str
    model: str
    steps: int
    calls: list[dict[str, Any]] = field(default_factory=list)
    unverified_urls: list[str] = field(default_factory=list)
    stopped_early: bool = False


def _dump(result: dict[str, Any] | ToolFailure) -> tuple[str, bool]:
    if isinstance(result, ToolFailure):
        return result.message, True
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + ' ... [truncated; narrow the request for more]"'
    return text, False


def _urls_in(value: Any) -> set[str]:
    """Every URL that appears in a JSON value (string leaves, scanned with the URL regex)."""
    if isinstance(value, str):
        return {u.rstrip(".,;") for u in _URL.findall(value)}
    if isinstance(value, dict):
        return set().union(*(_urls_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_urls_in(v) for v in value)) if value else set()
    return set()


def unverified_urls(markdown: str, evidence: list[str]) -> list[str]:
    """URLs in the answer that do not exactly match a URL from a successful tool result.

    ``evidence`` holds the JSON text of successful results only; error messages are
    not evidence even when they echo a URL.
    """
    known: set[str] = set()
    for text in evidence:
        try:
            known |= _urls_in(json.loads(text))
        except json.JSONDecodeError:
            known |= _urls_in(text)  # truncated result: fall back to scanning the text
    cited = {u.rstrip(".,;") for u in _URL.findall(markdown)} - {""}
    return sorted(cited - known)


async def _run_call(tb: Tools, call: ToolCall) -> ToolResult:
    target = split_tool_name(call.name)
    if target is None:
        return ToolResult(call.id, f"unknown tool {call.name!r}", is_error=True)
    server, tool = target
    content, is_error = _dump(await tb.call(server, tool, call.arguments))
    return ToolResult(call.id, content, is_error)


async def ask(
    question: str,
    tb: Tools,
    llm: LLMClient,
    on_event: EventSink | None = None,
    *,
    max_steps: int = MAX_STEPS,
    max_calls: int = MAX_CALLS,
) -> Answer:
    tools = await tb.tool_specs()
    if not tools:
        raise RuntimeError("no MCP server is reachable; nothing to call")
    session = llm.tool_session(SYSTEM, tools)
    async with llm_span(on_event, "llm1", llm.name, "decide") as span:
        turn = await session.start(question)
        span["calls"] = len(turn.calls)
    corpus: list[str] = []
    calls_made: list[dict[str, Any]] = []
    steps = 0
    stopped_early = False
    while turn.calls:
        if steps >= max_steps or len(calls_made) >= max_calls:
            stopped_early = True
            break
        steps += 1
        if turn.text:
            await emit(on_event, "text", text=turn.text)
        budget = max_calls - len(calls_made)
        runnable, refused = turn.calls[:budget], turn.calls[budget:]
        outcomes = list(await asyncio.gather(*(_run_call(tb, c) for c in runnable)))
        results = list(outcomes)
        results += [ToolResult(c.id, "call budget exhausted", is_error=True) for c in refused]
        corpus += [r.content for r in results[: len(outcomes)] if not r.is_error]
        calls_made += [{"name": c.name, "arguments": c.arguments} for c in runnable]
        async with llm_span(on_event, f"llm{steps + 1}", llm.name, "decide") as span:
            turn = await session.submit(results)
            span["calls"] = len(turn.calls)

    markdown = turn.text or ""
    if stopped_early or not markdown:
        stopped_early = True
        note = f"（已达到 {max_steps} 轮 / {max_calls} 次工具调用上限，回答可能不完整）"
        markdown = f"{markdown}\n\n> {note}".strip()
        await emit(on_event, "warning", message=note)

    unverified = unverified_urls(markdown, corpus)
    if unverified:
        markdown += "\n\n> ⚠ 以下链接未出现在任何工具结果中，未经验证：\n" + "\n".join(
            f"> - {u}" for u in unverified
        )
        await emit(on_event, "warning", message=f"{len(unverified)} 个链接未经工具结果验证")
    markdown += f"\n\n---\n\n生成方式：{llm.name} · 工具调用 {len(calls_made)} 次 / {steps} 轮"
    return Answer(markdown, llm.name, steps, calls_made, unverified, stopped_early)
