"""Feed endpoints and parsers for mining news.

Sources (all public RSS, no login):

* mining.com WordPress search feed ``/?s=<query>&feed=rss2`` - query-specific, full content
* mining.com commodity feeds ``/commodity/<slug>/feed/`` - latest items per commodity
* mining.com front-page feed ``/feed/`` - latest items site-wide
* Google News search RSS - broad coverage; links are Google redirects, so only the
  headline and publisher are available (``full_text_available=False``)
"""

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser

MINING_SEARCH = "https://www.mining.com/?s={query}&feed=rss2"
MINING_COMMODITY = "https://www.mining.com/commodity/{slug}/feed/"
MINING_FRONT = "https://www.mining.com/feed/"
GOOGLE_NEWS = (
    "https://news.google.com/rss/search?q={query}%20when:{days}d&hl=en-US&gl=US&ceid=US:en"
)

# Query words that map to a mining.com commodity feed.
COMMODITY_SLUGS: dict[str, str] = {
    "lithium": "lithium",
    "spodumene": "lithium",
    "锂": "lithium",
    "copper": "copper",
    "铜": "copper",
    "nickel": "nickel",
    "镍": "nickel",
    "zinc": "zinc",
    "gold": "gold",
    "iron": "iron-ore",
    "rare": "rare-earths",
    "uranium": "uranium",
    "cobalt": "cobalt",
}

_STOPWORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "news", "latest"}
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


class FeedError(ValueError):
    """The payload is not a parseable RSS/Atom feed."""


@dataclass(slots=True)
class FeedEntry:
    title: str
    url: str
    published_at: datetime
    summary: str
    content: str
    publisher: str
    full_text_available: bool
    feed_url: str
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha1(canonical_url(self.url).encode(), usedforsecurity=False).hexdigest()[
            :12
        ]


def search_feed_urls(query: str, days: int) -> list[str]:
    urls = [MINING_SEARCH.format(query=quote_plus(query))]
    slugs = dict.fromkeys(COMMODITY_SLUGS[t] for t in terms(query) if t in COMMODITY_SLUGS)
    urls += [MINING_COMMODITY.format(slug=s) for s in slugs]
    urls.append(MINING_FRONT)
    urls.append(GOOGLE_NEWS.format(query=quote_plus(query).replace("+", "%20"), days=days))
    return urls


def _is_cjk(word: str) -> bool:
    return any(0x4E00 <= ord(ch) <= 0x9FFF for ch in word)


def terms(query: str) -> list[str]:
    """Lower-cased search terms; single characters are kept only for CJK."""
    words = re.findall(r"\w+", query.lower())
    return [w for w in words if w not in _STOPWORDS and (len(w) > 1 or _is_cjk(w))]


# Query parameters that only track the click, never identify the article.
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "oc", "ref", "source"}


def canonical_url(url: str) -> str:
    """Normalise for dedupe: lower-case host, no trailing slash or fragment, tracking
    parameters dropped, remaining query parameters kept (``?p=101`` is an article id)."""
    parts = urlsplit(url)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))


def plain_text(fragment: str) -> str:
    return _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", fragment))).strip()


def _published(entry: Any) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


_KNOWN_PUBLISHERS = {"mining.com": "MINING.COM"}


def _publisher(link: str) -> str:
    """Name the publisher from the article's own host, never from the feed it came in."""
    host = (urlsplit(link).hostname or "").lower().removeprefix("www.")
    return _KNOWN_PUBLISHERS.get(host, host or "unknown")


def parse_feed(content: bytes, feed_url: str) -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    if parsed.get("bozo") and not parsed.entries:
        raise FeedError(f"not a valid feed: {feed_url}")
    is_google = urlsplit(feed_url).netloc == "news.google.com"
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        published = _published(e)
        link = e.get("link")
        title = plain_text(e.get("title", ""))
        if not (published and link and title):
            continue
        if is_google:
            publisher = e.get("source", {}).get("title") or "Google News"
            suffix = f" - {publisher}"
            title = title.removesuffix(suffix)
            summary, content_html = "", ""
        else:
            publisher = _publisher(link)
            summary = plain_text(e.get("summary", ""))
            content_html = (e.get("content") or [{}])[0].get("value", "")
        entries.append(
            FeedEntry(
                title=title,
                url=link,
                published_at=published,
                summary=summary,
                content=plain_text(content_html),
                publisher=publisher,
                full_text_available=not is_google,
                feed_url=feed_url,
                tags=[t.get("term", "") for t in e.get("tags", []) if t.get("term")],
            )
        )
    return entries
