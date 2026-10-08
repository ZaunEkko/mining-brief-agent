"""News search (multi-feed merge, dedupe, rank) and article extraction."""

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import trafilatura
from pydantic import BaseModel, Field

from mining_brief.common.http import CachedHttp, FetchError
from mining_brief.common.models import Provenance
from mining_brief.common.urls import UnsafeUrlError, ensure_public_url
from mining_brief.servers.news.sources import (
    FeedEntry,
    FeedError,
    canonical_url,
    parse_feed,
    search_feed_urls,
    terms,
)

logger = logging.getLogger(__name__)

FEED_TTL = timedelta(minutes=30)
ARTICLE_TTL = timedelta(days=7)
MAX_DAYS = 90
MAX_RESULTS = 50
SUMMARY_CHARS = 400
ARTICLE_MAX_BYTES = 10 * 1024 * 1024


class NewsError(Exception):
    """A request that cannot be served."""


class NewsItem(BaseModel):
    id: str
    title: str
    url: str
    publisher: str
    published_at: datetime
    summary: str = Field(description=f"Plain-text lead, at most {SUMMARY_CHARS} chars")
    score: float = Field(description="Relevance: IDF-weighted term hits, title 3x body")
    full_text_available: bool = Field(
        description="False for Google News redirect links, which fetch_article cannot open"
    )
    tags: list[str] = Field(default_factory=list)


class NewsSearchResult(BaseModel):
    query: str
    days: int
    items: list[NewsItem]
    sources: list[Provenance]
    notes: list[str] = Field(default_factory=list)


class Article(BaseModel):
    url: str
    title: str | None
    author: str | None
    published: str | None
    publisher: str | None
    text: str
    truncated: bool
    word_count: int
    provenance: Provenance


def _text(entry: FeedEntry) -> tuple[str, str]:
    return entry.title.lower(), f"{entry.summary} {entry.content}".lower()


def _term_weights(entries: list[FeedEntry], query_terms: list[str]) -> dict[str, float]:
    """IDF-style weights: a term found in few candidates ("pilbara") outweighs a
    term found in most of them ("lithium")."""
    texts = [" ".join(_text(e)) for e in entries]
    n = max(len(texts), 1)
    return {t: math.log(1 + n / (1 + sum(t in x for x in texts))) for t in query_terms}


def _score(entry: FeedEntry, weights: dict[str, float]) -> float:
    title, body = _text(entry)
    return round(
        sum(w * (3 if t in title else 1 if t in body else 0) for t, w in weights.items()), 3
    )


def _lead(entry: FeedEntry) -> str:
    text = entry.summary or entry.content
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1].rstrip() + "…"


def _title_key(title: str) -> str:
    return "".join(ch for ch in title.lower() if ch.isalnum())


def rank(entries: list[FeedEntry], query: str, days: int, now: datetime) -> list[NewsItem]:
    """Filter to the window, keep entries matching at least one term, dedupe, rank."""
    query_terms = terms(query)
    cutoff = now - timedelta(days=days)
    in_window = [e for e in entries if e.published_at >= cutoff]
    weights = _term_weights(in_window, query_terms)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    items: list[NewsItem] = []
    # Full-text sources first so a duplicate headline keeps the fetchable copy.
    for entry in sorted(in_window, key=lambda e: not e.full_text_available):
        score = _score(entry, weights)
        if query_terms and score == 0:
            continue
        url_key, title_key = canonical_url(entry.url), _title_key(entry.title)
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        items.append(
            NewsItem(
                id=entry.id,
                title=entry.title,
                url=entry.url,
                publisher=entry.publisher,
                published_at=entry.published_at,
                summary=_lead(entry),
                score=score,
                full_text_available=entry.full_text_available,
                tags=entry.tags,
            )
        )
    items.sort(key=lambda i: (i.score, i.full_text_available, i.published_at), reverse=True)
    return items


class NewsService:
    def __init__(self, http: CachedHttp) -> None:
        self._http = http

    async def search(self, query: str, days: int = 7, limit: int = 20) -> NewsSearchResult:
        query = query.strip()
        if not query:
            raise NewsError("query must not be empty")
        if not 1 <= days <= MAX_DAYS:
            raise NewsError(f"days must be between 1 and {MAX_DAYS}, got {days}")
        limit = max(1, min(limit, MAX_RESULTS))

        urls = search_feed_urls(query, days)
        results = await asyncio.gather(
            *(self._http.get(u, ttl=FEED_TTL) for u in urls), return_exceptions=True
        )

        entries: list[FeedEntry] = []
        sources: list[Provenance] = []
        notes: list[str] = []
        for url, result in zip(urls, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, FetchError):
                    raise result
                notes.append(f"feed unavailable: {url} ({result})")
                continue
            try:
                parsed = parse_feed(result.content, url)
            except FeedError as exc:
                notes.append(str(exc))
                continue
            entries += parsed
            sources.append(
                Provenance(
                    source=f"RSS · {urlsplit(url).netloc}",
                    source_url=url,
                    retrieved_at=result.retrieved_at,
                    data_mode=result.data_mode,
                )
            )
        if not sources:
            raise NewsError("all news feeds are unavailable: " + "; ".join(notes))

        # Anchor "last N days" to when the data was captured, so replayed caches and
        # fixtures answer relative to their snapshot instead of returning nothing.
        anchor = max(s.retrieved_at for s in sources)
        if datetime.now(UTC) - anchor > timedelta(days=1):
            notes.append(f"window anchored to snapshot time {anchor.isoformat(timespec='minutes')}")
        items = rank(entries, query, days, anchor)
        if not items:
            notes.append(f"no articles matched {query!r} in the last {days} days")
        return NewsSearchResult(
            query=query, days=days, items=items[:limit], sources=sources, notes=notes
        )

    async def fetch_article(self, url: str, max_chars: int = 8000) -> Article:
        try:
            ensure_public_url(url)
        except UnsafeUrlError as exc:
            raise NewsError(str(exc)) from exc
        if urlsplit(url).netloc == "news.google.com":
            raise NewsError(
                "Google News links are JavaScript redirects and cannot be fetched; "
                "use the headline as-is or search for the story on mining.com"
            )
        try:
            result = await self._http.get(
                url, ttl=ARTICLE_TTL, public_only=True, max_bytes=ARTICLE_MAX_BYTES
            )
        except (UnsafeUrlError, FetchError) as exc:
            raise NewsError(str(exc)) from exc
        doc = trafilatura.bare_extraction(result.text, url=url, with_metadata=True)
        data = doc.as_dict() if doc is not None and hasattr(doc, "as_dict") else (doc or {})
        text: str = (data.get("text") or "").strip()
        if len(text) < 200:
            raise NewsError(
                f"could not extract article text from {url} (paywall or non-article page?)"
            )
        truncated = len(text) > max_chars
        return Article(
            url=url,
            title=data.get("title"),
            author=data.get("author"),
            published=data.get("date"),
            publisher=data.get("sitename"),
            text=text[:max_chars],
            truncated=truncated,
            word_count=len(text.split()),
            provenance=Provenance(
                source=data.get("sitename") or urlsplit(url).netloc,
                source_url=url,
                retrieved_at=result.retrieved_at,
                data_mode=result.data_mode,
            ),
        )
