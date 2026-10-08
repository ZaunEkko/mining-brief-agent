"""Plan-and-execute orchestration.

plan -> parallel tool calls -> fetch top articles -> compose -> render. A failing
server only degrades its own section.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from mining_brief.agent.compose import (
    BriefContent,
    SourceRegistry,
    collect_news,
    maybe_llm,
    merge_quote,
    merge_risks,
    rule_risks,
    select_news,
)
from mining_brief.agent.events import EventSink, emit, llm_span
from mining_brief.agent.intent import BriefPlan, plan_brief
from mining_brief.agent.llm import LLMClient
from mining_brief.agent.render import facts_for_llm, render
from mining_brief.agent.toolbox import ServerName, Target, Toolbox, ToolFailure, Tools

logger = logging.getLogger(__name__)

ARTICLES_TO_FETCH = 3
ARTICLE_CHARS = 4000


@dataclass(frozen=True, slots=True)
class Brief:
    markdown: str
    plan: BriefPlan
    content: BriefContent


async def _gather_data(tb: Tools, plan: BriefPlan) -> dict[str, Any]:
    async def none() -> None:
        return None

    news, trend, quote, resources = await asyncio.gather(
        tb.call("news", "search", {"query": plan.news_query, "days": plan.news_days, "limit": 30}),
        tb.call("price", "get_trend", {"commodity": plan.commodity, "days": plan.trend_days})
        if plan.commodity
        else none(),
        tb.call("price", "get_price", {"commodity": plan.commodity}) if plan.commodity else none(),
        tb.call("pdf", "extract_resources", {"pdf_url": plan.report.url})
        if plan.report
        else none(),
    )
    articles: dict[str, dict[str, Any] | ToolFailure] = {}
    if isinstance(news, dict):
        aliases = plan.project.news_aliases if plan.project else ()
        chosen = [item for item, _ in select_news(news["items"], aliases)]
        urls = [i["url"] for i in chosen if i["full_text_available"]][:ARTICLES_TO_FETCH]
        fetched = await asyncio.gather(
            *(
                tb.call("news", "fetch_article", {"url": u, "max_chars": ARTICLE_CHARS})
                for u in urls
            )
        )
        articles = dict(zip(urls, fetched, strict=True))
    return {
        "news": news,
        "trend": trend,
        "quote": quote,
        "resources": resources,
        "articles": articles,
    }


def _modes(*payloads: Any) -> set[str]:
    modes: set[str] = set()
    for p in payloads:
        if isinstance(p, dict):
            if "provenance" in p:
                modes.add(p["provenance"]["data_mode"])
            for s in p.get("sources", []):
                modes.add(s["data_mode"])
    return modes


async def generate_brief(
    query: str,
    targets: dict[ServerName, Target],
    *,
    llm: LLMClient | None = None,
    timeout_s: float = 180.0,
    now: datetime | None = None,
    on_event: EventSink | None = None,
    toolbox: Toolbox | None = None,
) -> Brief:
    """Generate a brief; reuse ``toolbox`` connections when given, else open new ones."""
    plan = plan_brief(query)
    generated_at = now or datetime.now().astimezone()
    await emit(
        on_event,
        "plan",
        subject=plan.subject,
        project=plan.project.name if plan.project else None,
        commodity=plan.commodity,
        news_query=plan.news_query,
        news_days=plan.news_days,
        report=plan.report.url if plan.report else None,
    )
    if toolbox is not None:
        data = await _gather_data(toolbox.bind(on_event), plan)
    else:
        async with Toolbox(targets, timeout_s, on_event) as tb:
            data = await _gather_data(tb, plan)

    sources = SourceRegistry()
    aliases = plan.project.news_aliases if plan.project else ()
    news, material, news_failure = collect_news(data["news"], data["articles"], sources, aliases)
    news_notes = data["news"].get("notes", []) if isinstance(data["news"], dict) else []

    resources = data["resources"] if isinstance(data["resources"], dict) else None
    resources_failure = (
        str(data["resources"]) if isinstance(data["resources"], ToolFailure) else None
    )
    resources_cite = None
    report_age = None
    if plan.report:
        resources_cite = sources.add(
            plan.report.url, plan.report.title, "ASX", plan.report.published
        )
        report_age = (generated_at.date() - date.fromisoformat(plan.report.published)).days

    prices: list[tuple[dict[str, Any], int]] = []
    price_failures = [str(r) for r in (data["trend"], data["quote"]) if isinstance(r, ToolFailure)]
    if isinstance(data["trend"], dict):
        trend = data["trend"]
        if isinstance(data["quote"], dict):
            trend = merge_quote(trend, data["quote"])
        prov = trend["provenance"]
        cite = sources.add(prov["source_url"], prov["source"], None, trend["end_date"])
        prices.append((trend, cite))

    risks = rule_risks(news, material, prices, resources, report_age)
    content = BriefContent(
        overview=None,
        news=news,
        news_failure=news_failure,
        news_notes=news_notes,
        resources=resources,
        resources_failure=resources_failure,
        resources_cite=resources_cite,
        prices=prices,
        prices_failure="; ".join(price_failures) or (None if plan.commodity else "未识别到品种"),
        risks=risks,
        sources=sources,
        data_modes={
            "新闻": _modes(data["news"], *data["articles"].values()),
            "储量": _modes(data["resources"]),
            "价格": _modes(data["trend"], data["quote"]),
        },
        generator="deterministic",
    )
    if llm is not None:
        async with llm_span(on_event, "compose", llm.name, "compose"):
            overview, news, llm_risks, generator, warnings = await maybe_llm(
                llm, plan.subject, material, facts_for_llm(content), news, sources
            )
    else:
        overview, news, llm_risks, generator, warnings = await maybe_llm(
            None, plan.subject, material, facts_for_llm(content), news, sources
        )
    content.overview, content.news, content.generator = overview, news, generator
    content.warnings += warnings
    for warning in warnings:
        await emit(on_event, "warning", message=warning)
    # Rule-based data-quality risks always stay; LLM risks replace the keyword heuristics.
    if llm_risks:
        content.risks = merge_risks(llm_risks, risks)
    return Brief(markdown=render(plan, content, generated_at), plan=plan, content=content)
