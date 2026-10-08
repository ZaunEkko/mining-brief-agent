"""Known projects: how a user's wording maps to commodities, news queries and reports.

The agent only reaches data through MCP tools; this catalog just tells it which
arguments to pass. Report URLs point at the ASX announcements CDN (public, no login).
"""

import re
from dataclasses import dataclass, field

ASX_FILE = "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/{key}"


@dataclass(frozen=True, slots=True)
class Report:
    title: str
    url: str
    published: str
    code: str


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    owner: str
    region: str
    commodity: str
    news_query: str
    aliases: tuple[str, ...]
    # Words that mark a news item as being about this project (stricter than
    # ``aliases``: "pilbara" alone also matches the region's iron ore news).
    news_aliases: tuple[str, ...] = ()
    reports: tuple[Report, ...] = field(default_factory=tuple)


PROJECTS: tuple[Project, ...] = (
    Project(
        name="Pilgangoora",
        owner="PLS Group (formerly Pilbara Minerals, ASX: PLS)",
        region="Pilbara, Western Australia",
        commodity="lithium_carbonate",
        news_query="PLS lithium",
        aliases=("pilbara", "pilgangoora", "pls", "pilbara minerals", "皮尔巴拉"),
        news_aliases=("pilgangoora", "pls", "pilbara minerals"),
        reports=(
            Report(
                title="Pilgangoora Mineral Resource Update",
                url=ASX_FILE.format(key="2924-02955435-6A1268075"),
                published="2025-06-11",
                code="JORC",
            ),
        ),
    ),
)


def _mentions(text: str, alias: str) -> bool:
    if alias.isascii():  # whole words only, so "pls" does not match "samples"
        return re.search(rf"\b{re.escape(alias)}\b", text) is not None
    return alias in text


def mentions_any(text: str, aliases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(_mentions(lowered, a) for a in aliases)


def find_project(text: str) -> Project | None:
    lowered = text.lower()
    for project in PROJECTS:
        if any(_mentions(lowered, alias) for alias in project.aliases):
            return project
    return None
