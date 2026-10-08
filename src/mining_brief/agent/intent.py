"""Turn the user's request into concrete tool arguments."""

import re
from dataclasses import dataclass

from mining_brief.agent.catalog import Project, Report, find_project

DEFAULT_NEWS_DAYS = 7
DEFAULT_TREND_DAYS = 30

# Keyword -> commodity argument for lme-price-mcp (the server resolves aliases itself).
_COMMODITY_WORDS: dict[str, str] = {
    "锂": "lithium_carbonate",
    "lithium": "lithium_carbonate",
    "spodumene": "lithium_carbonate",
    "铜": "copper",
    "copper": "copper",
    "镍": "nickel",
    "nickel": "nickel",
    "锌": "zinc",
    "zinc": "zinc",
    "铝": "aluminium",
    "alumin": "aluminium",
    "锡": "tin",
    "tin ": "tin",
    "铅": "lead",
}
_COMMODITY_EN = {
    "lithium_carbonate": "lithium",
    "copper": "copper",
    "nickel": "nickel",
    "zinc": "zinc",
    "aluminium": "aluminium",
    "tin": "tin",
    "lead": "lead",
}
COMMODITY_LABELS = {
    "lithium_carbonate": "碳酸锂（GFEX 主力连续）",
    "copper": "LME 铜",
    "zinc": "LME 锌",
    "nickel": "LME 镍",
    "aluminium": "LME 铝",
    "lead": "LME 铅",
    "tin": "LME 锡",
}
_DAYS = re.compile(r"(?:近|过去|last)\s*(\d{1,2})\s*(?:天|日|days?)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BriefPlan:
    query: str
    subject: str
    project: Project | None
    commodity: str | None
    news_query: str
    news_days: int
    trend_days: int
    report: Report | None


def _commodity(text: str) -> str | None:
    lowered = text.lower()
    return next((c for word, c in _COMMODITY_WORDS.items() if word in lowered), None)


def plan_brief(query: str) -> BriefPlan:
    match = _DAYS.search(query)
    days = int(match.group(1)) if match else DEFAULT_NEWS_DAYS
    project = find_project(query)
    if project:
        return BriefPlan(
            query=query,
            subject=project.name,
            project=project,
            commodity=project.commodity,
            news_query=project.news_query,
            news_days=days,
            trend_days=DEFAULT_TREND_DAYS,
            report=project.reports[0] if project.reports else None,
        )
    commodity = _commodity(query)
    latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9\-]+", query))
    news_query = latin or (_COMMODITY_EN.get(commodity, "") if commodity else "") or "mining"
    return BriefPlan(
        query=query,
        subject=latin or (_COMMODITY_EN.get(commodity, "mining") if commodity else "mining"),
        project=None,
        commodity=commodity,
        news_query=news_query,
        news_days=days,
        trend_days=DEFAULT_TREND_DAYS,
        report=None,
    )
