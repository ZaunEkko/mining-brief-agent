"""Turn raw tool results into brief content: citations, summaries and risk flags.

Numbers (resources, prices) are always rendered straight from tool output. An LLM,
when configured, only writes prose and may only cite sources that exist; anything
else it returns is dropped.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from mining_brief.agent.catalog import mentions_any
from mining_brief.agent.intent import COMMODITY_LABELS
from mining_brief.agent.llm import LLMClient, LLMError
from mining_brief.agent.toolbox import ToolFailure

logger = logging.getLogger(__name__)

NEWS_PER_GROUP = 4
SUMMARY_CHARS = 260
PRICE_MOVE_PCT = 10.0
VOLATILITY_PCT = 15.0
REPORT_STALE_DAYS = 365

# Risk label -> whole-word pattern matched against article title + text.
RISK_PATTERNS: dict[str, str] = {
    "停产/暂停": r"\bhalt(?:s|ed|ing)?\b|\bsuspen(?:d|ds|ded|sion)\b|\bshut(?:s|ting)?(?: down)?\b"
    r"|\bshutdowns?\b|\bcare and maintenance\b",
    "减产": r"\bcurtail(?:s|ed|ment|ments)?\b|\boutput cuts?\b",
    "罢工/劳工": r"\bstrikes?\b|\blay-?offs?\b|\bjob cuts\b",
    "税费/特许权": r"\broyalt(?:y|ies)\b|\btax(?:es|ation)?\b",
    "贸易限制": r"\btariffs?\b|\bexport (?:bans?|restrictions?|controls?)\b|\bbanned\b",
    "司法/诉讼": r"\bcourts?\b|\blawsuits?\b|\blitigation\b",
    "资产减值": r"\bimpairments?\b|\bwrite-?downs?\b",
    "极端天气": r"\bcyclones?\b|\bflood(?:s|ing)?\b",
    "安全事故": r"\bfatal(?:ity|ities)?\b|\bexplosion\b",
    "供应过剩": r"\boversupply\b|\bglut\b|\bsurplus\b",
}


@dataclass(slots=True)
class Source:
    n: int
    title: str
    url: str
    publisher: str | None = None
    date: str | None = None


@dataclass(slots=True)
class SourceRegistry:
    items: list[Source] = field(default_factory=list)

    def add(
        self, url: str, title: str, publisher: str | None = None, date: str | None = None
    ) -> int:
        for s in self.items:
            if s.url == url:
                return s.n
        source = Source(n=len(self.items) + 1, title=title, url=url, publisher=publisher, date=date)
        self.items.append(source)
        return source.n

    def valid(self, numbers: list[int]) -> list[int]:
        known = {s.n for s in self.items}
        return [n for n in numbers if n in known]


@dataclass(slots=True)
class NewsPoint:
    title: str
    summary: str
    publisher: str
    date: str
    cite: int
    group: str = "market"  # "project" | "market"


@dataclass(slots=True)
class Risk:
    text: str
    cites: list[int] = field(default_factory=list)
    kind: str = "llm"  # "news" | "price" | "data" for rule-based risks


@dataclass(slots=True)
class BriefContent:
    overview: str | None
    news: list[NewsPoint]
    news_failure: str | None
    news_notes: list[str]
    resources: dict[str, Any] | None
    resources_failure: str | None
    resources_cite: int | None
    prices: list[tuple[dict[str, Any], int]]
    prices_failure: str | None
    risks: list[Risk]
    sources: SourceRegistry
    data_modes: dict[str, set[str]]
    generator: str
    warnings: list[str] = field(default_factory=list)


def _sentences(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
        if out and len(out) + len(sentence) > limit:
            break
        out = f"{out} {sentence}".strip()
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _article_lead(article: dict[str, Any] | None, title: str) -> str | None:
    if not article:
        return None
    lines = [ln.strip() for ln in article["text"].splitlines() if ln.strip()]
    if lines and lines[0] == title:
        lines = lines[1:]
    return _sentences(" ".join(lines)) if lines else None


def select_news(
    items: list[dict[str, Any]], project_aliases: tuple[str, ...]
) -> list[tuple[dict[str, Any], str]]:
    """Split ranked search items into project / market groups, best first."""
    project: list[dict[str, Any]] = []
    market: list[dict[str, Any]] = []
    for item in items:
        text = f"{item['title']} {item['summary']}"
        target = project if project_aliases and mentions_any(text, project_aliases) else market
        target.append(item)
    return [(i, "project") for i in project[:NEWS_PER_GROUP]] + [
        (i, "market") for i in market[:NEWS_PER_GROUP]
    ]


def collect_news(
    search: dict[str, Any] | ToolFailure,
    articles: dict[str, dict[str, Any] | ToolFailure],
    sources: SourceRegistry,
    project_aliases: tuple[str, ...] = (),
) -> tuple[list[NewsPoint], list[dict[str, Any]], str | None]:
    """News points for the brief plus the raw material (for the LLM).

    With project aliases, items naming the project form the "project" group and
    the rest the "market" group; each group keeps its best-ranked items.
    """
    if isinstance(search, ToolFailure):
        return [], [], str(search)
    points: list[NewsPoint] = []
    material: list[dict[str, Any]] = []
    for item, group in select_news(search["items"], project_aliases):
        date = item["published_at"][:10]
        cite = sources.add(item["url"], item["title"], item["publisher"], date)
        article = articles.get(item["url"])
        body = article if isinstance(article, dict) else None
        lead = _article_lead(body, item["title"]) or item["summary"] or "（仅有标题，正文不可获取）"
        points.append(
            NewsPoint(
                title=item["title"],
                summary=_sentences(lead),
                publisher=item["publisher"],
                date=date,
                cite=cite,
                group=group,
            )
        )
        material.append(
            {
                "id": cite,
                "title": item["title"],
                "publisher": item["publisher"],
                "date": date,
                "text": (body["text"][:3000] if body else item["summary"]),
            }
        )
    return points, material, None


def merge_risks(llm_risks: list[Risk], rules: list[Risk]) -> list[Risk]:
    """LLM risks first; keep data-quality rules, and price rules the LLM did not cover."""
    llm_cites = {n for r in llm_risks for n in r.cites}
    kept = [
        r for r in rules if r.kind == "data" or (r.kind == "price" and not set(r.cites) & llm_cites)
    ]
    return llm_risks + kept


def rule_risks(
    news: list[NewsPoint],
    material: list[dict[str, Any]],
    prices: list[tuple[dict[str, Any], int]],
    resources: dict[str, Any] | None,
    report_age_days: int | None,
) -> list[Risk]:
    risks: list[Risk] = []
    by_label: dict[str, list[int]] = {}
    for m in material:
        haystack = f"{m['title']} {m['text']}".lower()
        for label, pattern in RISK_PATTERNS.items():
            if re.search(pattern, haystack):
                by_label.setdefault(label, [])
                if m["id"] not in by_label[label]:
                    by_label[label].append(m["id"])
    for label, cites in by_label.items():
        risks.append(
            Risk(f"新闻中出现「{label}」相关信号，需关注对供应或成本的影响", cites[:3], "news")
        )

    for trend, cite in prices:
        pct = trend["change_pct"]
        name = COMMODITY_LABELS.get(trend["commodity"], trend["commodity"])
        window = trend["window_days"]
        if pct <= -PRICE_MOVE_PCT:
            text = f"{name} 近 {window} 天下跌 {abs(pct):.1f}%，价格下行压力"
            risks.append(Risk(text, [cite], "price"))
        elif pct >= PRICE_MOVE_PCT:
            text = f"{name} 近 {window} 天上涨 {pct:.1f}%，注意追高与回调风险"
            risks.append(Risk(text, [cite], "price"))
        spread = (trend["high"] - trend["low"]) / trend["mean"] * 100 if trend["mean"] else 0
        if spread >= VOLATILITY_PCT:
            risks.append(Risk(f"{name} 区间振幅 {spread:.1f}%，波动显著", [cite], "price"))

    if resources is not None:
        if resources["abstain"]:
            risks.append(
                Risk("资源量表未通过一致性校验，已拒绝展示具体数字，需人工核对原报告", [], "data")
            )
        if report_age_days is not None and report_age_days > REPORT_STALE_DAYS:
            risks.append(
                Risk(
                    f"资源量报告发布已 {report_age_days} 天，可能未反映最新钻探与开采消耗",
                    [],
                    "data",
                )
            )
    return risks


class _NewsSummary(BaseModel):
    id: int
    summary: str


class _RiskClaim(BaseModel):
    text: str
    sources: list[int]


class _ComposeOutput(BaseModel):
    overview: str = ""
    news: list[_NewsSummary]
    risks: list[_RiskClaim]


def merge_quote(trend: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Keep trend statistics internally consistent; only note a newer quote.

    The trend's change / high / low / mean all describe its own window, so splicing a
    later quote into ``last`` would contradict them.
    """
    if str(quote.get("trading_date")) <= str(trend.get("end_date")):
        return trend
    note = (
        f"最新报价 {quote['price']:,.0f} {quote.get('unit', '')}（{quote['trading_date']}）"
        f"晚于走势窗口末日 {trend['end_date']}，涨跌幅按窗口计算"
    )
    return trend | {"notes": [*trend.get("notes", []), note]}


_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "summary": {"type": "string"}},
                "required": ["id", "summary"],
                "additionalProperties": False,
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "news", "risks"],
    "additionalProperties": False,
}

_SYSTEM = (
    "你是矿业研究分析师，为中文读者撰写每日简报。只能使用用户提供的材料，不得引入材料之外的事实或数字。"
    "每条新闻摘要用 1-2 句中文概括要点；风险提示必须引用材料中的来源编号（sources 字段）。"
    "overview 用 2-3 句话概括今日要点，不要罗列具体价格或资源量数字（这些由系统单独展示）。"
    "以分析师口吻直接陈述，不要写「材料」「所提供」「据标题」之类的措辞；"
    "某条新闻只有标题时，摘要只复述标题含义，并以「（仅标题）」结尾，不要推测细节。"
)


async def llm_compose(
    llm: LLMClient,
    subject: str,
    material: list[dict[str, Any]],
    facts: dict[str, Any],
    news: list[NewsPoint],
    sources: SourceRegistry,
) -> tuple[str | None, list[NewsPoint], list[Risk]]:
    """Rewrite summaries and risks with the LLM; invalid citations are discarded."""
    user = json.dumps(
        {"subject": subject, "news": material, "facts": facts}, ensure_ascii=False, default=str
    )
    raw = await llm.complete_json(_SYSTEM, user, _LLM_SCHEMA)
    try:
        data = _ComposeOutput.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(f"model output does not match the composition schema: {exc}") from exc
    summaries = {n.id: n.summary.strip() for n in data.news if n.summary.strip()}
    rewritten = [
        NewsPoint(p.title, summaries.get(p.cite, p.summary), p.publisher, p.date, p.cite, p.group)
        for p in news
    ]
    risks: list[Risk] = []
    for r in data.risks:
        cites = sources.valid(r.sources)
        if r.text.strip() and cites:  # uncited claims are dropped
            risks.append(Risk(r.text.strip(), cites))
    overview = data.overview.strip() or None
    return overview, rewritten, risks


async def maybe_llm(
    llm: LLMClient | None,
    subject: str,
    material: list[dict[str, Any]],
    facts: dict[str, Any],
    news: list[NewsPoint],
    sources: SourceRegistry,
) -> tuple[str | None, list[NewsPoint], list[Risk], str, list[str]]:
    """LLM composition when available; deterministic content otherwise."""
    if llm is None:
        return None, news, [], "deterministic (no LLM configured)", []
    try:
        overview, rewritten, risks = await llm_compose(llm, subject, material, facts, news, sources)
    except LLMError as exc:
        logger.warning("LLM composition failed, using deterministic output: %s", exc)
        return (
            None,
            news,
            [],
            f"deterministic (LLM failed: {exc})",
            [f"LLM 调用失败，已降级：{exc}"],
        )
    return overview, rewritten, risks, llm.name, []
